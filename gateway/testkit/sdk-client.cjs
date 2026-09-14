'use strict';
/**
 * openai-node at DEFAULT settings against a base URL; prints one JSON line.
 * usage: node sdk-client.cjs SDK_DIR BASE_URL MODE MODEL
 */
const path = require('node:path');

const [sdkDir, baseURL, mode, model] = process.argv.slice(2);
const OpenAI = require(path.join(sdkDir, 'node_modules', 'openai'));
const { version } = require(path.join(sdkDir, 'node_modules', 'openai', 'package.json'));

(async () => {
  const client = new OpenAI({ apiKey: 'sk-test-gateway', baseURL }); // defaults: timeout 600000, maxRetries 2
  const out = { sdk: `node-${version}`, mode };
  const started = Date.now();
  try {
    let text = '';
    if (mode === 'chat-sync') {
      const r = await client.chat.completions.create({ model, messages: [{ role: 'user', content: 'hi' }] });
      text = r.choices[0].message.content;
    } else if (mode === 'chat-stream') {
      const stream = await client.chat.completions.create({ model, messages: [{ role: 'user', content: 'hi' }], stream: true });
      for await (const chunk of stream) text += chunk.choices?.[0]?.delta?.content ?? '';
    } else if (mode === 'responses-stream') {
      const stream = await client.responses.create({ model, input: 'hi', stream: true });
      const seqs = [];
      for await (const event of stream) {
        seqs.push(event.sequence_number);
        if (event.type === 'response.output_text.delta') text += event.delta;
      }
      out.seqs_contiguous = seqs.every((s, i) => s === i + 1);
    } else if (mode === 'responses-sync') {
      text = (await client.responses.create({ model, input: 'hi' })).output_text;
    } else {
      throw new Error(`unknown mode ${mode}`);
    }
    Object.assign(out, { ok: true, text });
  } catch (err) {
    Object.assign(out, { ok: false, error: `${err.constructor.name}: ${err.message}` });
  }
  out.elapsed_s = Math.round((Date.now() - started) / 10) / 100;
  process.stdout.write(`${JSON.stringify(out)}\n`);
})();
