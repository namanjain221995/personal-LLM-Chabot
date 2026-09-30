// whisper-large-v3 on the worker's CPU cores: the decoding half of compose/whisper-cpu/server.py.
//
// ONE LONG-LIVED PROCESS HOLDS THE MODEL. server.py starts it once, writes one request at a time
// on its stdin and reads one reply from its stdout. Keeping the decoder in its own process keeps
// the HTTP side identical to compose/whisper/server.py (FastAPI, ffmpeg, the same limits and the
// same error shapes) and lets a crash in native code end one request instead of the service.
//
// PROTOCOL (little-endian, one request in flight):
//   request  one JSON line: {"n_samples": N, "language": "en" | null, "timestamps": bool,
//                            "gate": bool, "threshold": 0.6}
//            then exactly N float32 samples, 16 kHz mono (ffmpeg has already decoded them)
//   reply    one JSON line: {"ok": true, "gated": bool, "no_speech_prob": p, "language": "en",
//                            "language_name": "english", "segments": [{"t0", "t1", "text"}],
//                            "prepass_ms", "decode_ms", "windows"}
//            or {"ok": false, "error": "..."}
//   startup  one JSON line: {"ready": true, ...} or {"ready": false, "error": "..."} then exit 1
//
// WHAT IS DECODED, AND WHY IT MATCHES THE GPU REPLICAS. compose/whisper/server.py runs the
// transformers pipeline with its defaults, and those defaults are reproduced here one by one:
//   * greedy, temperature 0, NO temperature fallback (temperature_inc = 0);
//   * no conditioning on previous text, in a call or across windows (no_context, n_max_text_ctx 0);
//   * task transcribe, never translate;
//   * blank and non-speech tokens suppressed at the start / throughout (suppress_blank,
//     suppress_nst: transformers' begin_suppress_tokens and suppress_tokens);
//   * the first timestamp no later than 1.0 s (max_initial_ts, transformers'
//     max_initial_timestamp_index 50);
//   * timestamps only when the caller asks for segments or the clip is 30 s or longer;
//   * sequential long form: whisper.cpp slides its 30 s window by the model's own timestamps, as
//     the transformers pipeline does when `chunk_length_s` is not passed;
//   * NO per-window silence skipping (no_speech_thold 2.0 can never be met), because the
//     transformers pipeline does not skip windows either.
//
// THE SILENCE GATE AND THE LANGUAGE ARE MEASURED THE WAY THE GPU REPLICA MEASURES THEM, and not
// the way whisper.cpp does. server.py's `_no_speech_probability` runs the model on the first 30 s
// with the decoder given <|startoftranscript|> alone and reads P(<|nospeech|>) at that position.
// whisper.cpp's own `no_speech_prob` is read after the whole prompt (<|sot|><|lang|><|task|>...),
// which is a different number, so it is not used. The pre-pass below does exactly what server.py
// does, and the same logits give the language (argmax over the language tokens at that position,
// which is what transformers' detect_language does).
#include "whisper.h"
#include "json.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

using json = nlohmann::json;

static const int SAMPLE_RATE = 16000;

static double ms_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

static void emit(const json & j) {
    // One line per message. `replace` keeps a split multi-byte character (whisper can end a
    // segment inside one) from turning the whole reply into an exception.
    std::cout << j.dump(-1, ' ', false, json::error_handler_t::replace) << '\n' << std::flush;
}

static void silence_logs(ggml_log_level, const char *, void *) {}

int main(int argc, char ** argv) {
    std::string model_path;
    int n_threads = 8;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--model" && i + 1 < argc) { model_path = argv[++i]; }
        else if (a == "--threads" && i + 1 < argc) { n_threads = std::max(1, std::atoi(argv[++i])); }
    }
    if (model_path.empty()) {
        emit({{"ready", false}, {"error", "usage: wcpp-worker --model <ggml file> [--threads N]"}});
        return 1;
    }
    // stdout carries the protocol; whisper.cpp logs to stderr, which server.py forwards to the
    // container log at startup and otherwise discards.
    if (std::getenv("WCPP_QUIET")) { whisper_log_set(silence_logs, nullptr); }

    const auto t_load = std::chrono::steady_clock::now();
    whisper_context_params cparams = whisper_context_default_params();
    cparams.use_gpu = false;
    whisper_context * ctx = whisper_init_from_file_with_params_no_state(model_path.c_str(), cparams);
    if (ctx == nullptr) {
        emit({{"ready", false}, {"error", "could not load " + model_path}});
        return 1;
    }
    whisper_state * state = whisper_init_state(ctx);
    if (state == nullptr) {
        emit({{"ready", false}, {"error", "could not allocate the decoder state"}});
        return 1;
    }
    const int n_vocab = whisper_n_vocab(ctx);
    const whisper_token sot = whisper_token_sot(ctx);
    const whisper_token nosp = whisper_token_nosp(ctx);
    const int lang_max = whisper_lang_max_id();
    emit({{"ready", true}, {"load_ms", ms_since(t_load)}, {"threads", n_threads},
          {"n_vocab", n_vocab}, {"system_info", whisper_print_system_info()}});

    std::string line;
    std::vector<float> pcm;
    std::vector<double> probs;
    while (std::getline(std::cin, line)) {
        json req;
        try {
            req = json::parse(line);
        } catch (const std::exception & e) {
            emit({{"ok", false}, {"error", std::string("bad request header: ") + e.what()}});
            return 2;  // the stream is out of step; server.py restarts the worker
        }
        const long long n = req.value("n_samples", 0LL);
        if (n <= 0 || n > 4LL * 3600 * SAMPLE_RATE) {
            emit({{"ok", false}, {"error", "n_samples out of range"}});
            return 2;
        }
        pcm.resize((size_t) n);
        std::cin.read(reinterpret_cast<char *>(pcm.data()), (std::streamsize) (n * sizeof(float)));
        if (!std::cin) {
            emit({{"ok", false}, {"error", "short read of the audio"}});
            return 2;
        }
        const bool gate = req.value("gate", true);
        const bool timestamps = req.value("timestamps", false);
        const double threshold = req.value("threshold", 0.6);
        std::string forced;
        if (req.contains("language") && req["language"].is_string()) {
            forced = req["language"].get<std::string>();
        }
        try {
            // -- pre-pass: the first 30 s, decoder given <|startoftranscript|> alone ------------
            // Skipped only when there is nothing to learn from it: no gate asked for and the
            // language forced. The GPU replica skips the same encoder pass in that case.
            const auto t_pre = std::chrono::steady_clock::now();
            double no_speech_prob = 0.0;
            int lang_id = -1;
            if (gate || forced.empty()) {
                const int n30 = (int) std::min<long long>(n, 30LL * SAMPLE_RATE);
                if (whisper_pcm_to_mel_with_state(ctx, state, pcm.data(), n30, n_threads) != 0) {
                    throw std::runtime_error("log-mel spectrogram failed");
                }
                if (whisper_encode_with_state(ctx, state, 0, n_threads) != 0) {
                    throw std::runtime_error("encoder failed");
                }
                if (whisper_decode_with_state(ctx, state, &sot, 1, 0, n_threads) != 0) {
                    throw std::runtime_error("decoder failed on <|startoftranscript|>");
                }
                const float * logits = whisper_get_logits_from_state(state);
                float lmax = -INFINITY;
                for (int i = 0; i < n_vocab; ++i) { lmax = std::max(lmax, logits[i]); }
                double sum = 0.0;
                probs.assign((size_t) n_vocab, 0.0);
                for (int i = 0; i < n_vocab; ++i) { probs[i] = std::exp((double) logits[i] - lmax); sum += probs[i]; }
                no_speech_prob = probs[nosp] / sum;
                float best = -INFINITY;
                for (int id = 0; id <= lang_max; ++id) {
                    const whisper_token t = whisper_token_lang(ctx, id);
                    if (t >= 0 && t < n_vocab && logits[t] > best) { best = logits[t]; lang_id = id; }
                }
            }
            const double prepass_ms = ms_since(t_pre);
            if (gate && no_speech_prob > threshold) {
                emit({{"ok", true}, {"gated", true}, {"no_speech_prob", no_speech_prob},
                      {"prepass_ms", prepass_ms}, {"decode_ms", 0.0}, {"segments", json::array()}});
                continue;
            }
            std::string code;
            if (!forced.empty()) {
                const int id = whisper_lang_id(forced.c_str());
                if (id < 0) { throw std::runtime_error("unsupported language '" + forced + "'"); }
                code = whisper_lang_str(id);
            } else {
                if (lang_id < 0) { throw std::runtime_error("no language token scored"); }
                code = whisper_lang_str(lang_id);
            }
            const int final_id = whisper_lang_id(code.c_str());

            // -- the transcription: production's decode settings, one by one ----------------------
            whisper_full_params wp = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
            wp.n_threads = n_threads;
            wp.n_max_text_ctx = 0;
            wp.offset_ms = 0;
            wp.duration_ms = 0;
            wp.translate = false;
            wp.no_context = true;
            wp.no_timestamps = !timestamps;
            wp.single_segment = false;
            wp.print_special = false;
            wp.print_progress = false;
            wp.print_realtime = false;
            wp.print_timestamps = false;
            wp.token_timestamps = false;
            wp.max_len = 0;
            wp.split_on_word = false;
            wp.max_tokens = 0;
            wp.debug_mode = false;
            wp.audio_ctx = 0;
            wp.tdrz_enable = false;
            wp.suppress_regex = nullptr;
            wp.initial_prompt = nullptr;
            wp.carry_initial_prompt = false;
            wp.prompt_tokens = nullptr;
            wp.prompt_n_tokens = 0;
            wp.language = code.c_str();
            wp.detect_language = false;
            wp.suppress_blank = true;
            wp.suppress_nst = true;
            wp.temperature = 0.0f;
            wp.max_initial_ts = 1.0f;
            wp.length_penalty = -1.0f;
            wp.temperature_inc = 0.0f;
            wp.entropy_thold = 2.4f;
            wp.logprob_thold = -1.0f;
            wp.no_speech_thold = 2.0f;
            wp.greedy.best_of = 1;
            wp.beam_search.beam_size = 1;
            wp.vad = false;

            const auto t_dec = std::chrono::steady_clock::now();
            int windows = 0;
            wp.encoder_begin_callback = [](whisper_context *, whisper_state *, void * user) {
                ++*static_cast<int *>(user);
                return true;
            };
            wp.encoder_begin_callback_user_data = &windows;
            if (whisper_full_with_state(ctx, state, wp, pcm.data(), (int) n) != 0) {
                throw std::runtime_error("whisper_full failed");
            }
            json segments = json::array();
            const int n_seg = whisper_full_n_segments_from_state(state);
            for (int i = 0; i < n_seg; ++i) {
                const char * text = whisper_full_get_segment_text_from_state(state, i);
                segments.push_back({
                    {"t0", whisper_full_get_segment_t0_from_state(state, i) / 100.0},
                    {"t1", whisper_full_get_segment_t1_from_state(state, i) / 100.0},
                    {"text", text ? text : ""},
                });
            }
            emit({{"ok", true}, {"gated", false}, {"no_speech_prob", no_speech_prob},
                  {"language", code}, {"language_name", whisper_lang_str_full(final_id)},
                  {"segments", segments}, {"prepass_ms", prepass_ms},
                  {"decode_ms", ms_since(t_dec)}, {"windows", windows}});
        } catch (const std::exception & e) {
            emit({{"ok", false}, {"error", e.what()}});
        }
    }
    whisper_free_state(state);
    whisper_free(ctx);
    return 0;
}
