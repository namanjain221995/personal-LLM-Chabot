"""Acceptance tests for the autopilot runner (MASTER_PROMPT.md §5.5, items 1-5).

They run ops/autopilot/autopilot.py against a stub `claude` (tests/stub_claude.py)
with time scaled down 1000x, so they cost no usage and finish in seconds.
Run: python3 -m unittest discover -s ops/autopilot/tests -v
Items 6 (systemd restart) and 7 (guard layers in a real session) are run by
hand; see docs/ai-platform-upgrade/IMPLEMENTATION_STATUS.md.
"""

import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(os.path.dirname(HERE), "autopilot.py")
STUB = os.path.join(HERE, "stub_claude.py")

spec = importlib.util.spec_from_file_location("autopilot", RUNNER)
autopilot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autopilot)

MASTER = "# x\n\n```yaml\nDEV_BRANCH: autopilot/dev\nMAX_AUTONOMOUS_DAYS: 14\nAUTOPILOT_MODEL: opus\nAUTOPILOT_EFFORT: xhigh\nAUTOPILOT_PAUSE_WINDOWS: []\n```\n"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def read_json(path):
    return json.loads(read(path))


class RunnerHarness(unittest.TestCase):
    scale = "0.001"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ap-runner-")
        self.ap = os.path.join(self.tmp, "ap")
        self.wt = os.path.join(self.tmp, "wt")
        docs = os.path.join(self.wt, "docs/ai-platform-upgrade")
        os.makedirs(docs)
        os.makedirs(self.ap)
        with open(os.path.join(docs, "RESUME.md"), "w") as fh:
            fh.write("STATUS: IN PROGRESS\n")
        with open(os.path.join(docs, "TASK_BOARD.md"), "w") as fh:
            fh.write("| A-01 | something | READY | x |\n")
        with open(os.path.join(self.ap, "MASTER_PROMPT.md"), "w") as fh:
            fh.write(MASTER)
        with open(os.path.join(self.ap, "prompt.md"), "w") as fh:
            fh.write("cycle prompt")
        with open(os.path.join(self.ap, "test-db.vars"), "w") as fh:
            fh.write("TEST_DATABASE_ALLOWED_HOSTS=192.0.2.20:15432\n")
        self.scenario = os.path.join(self.tmp, "scenario.json")
        self.calls = os.path.join(self.tmp, "calls.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **extra):
        e = dict(os.environ)
        e.update({
            "AP_HOME": self.ap, "AP_WORKTREE": self.wt, "AP_CLAUDE": STUB,
            "AP_SETTINGS": os.path.join(self.ap, "settings.json"), "AP_PROMPT_FILE": os.path.join(self.ap, "prompt.md"),
            "AP_MASTER": os.path.join(self.ap, "MASTER_PROMPT.md"), "AP_TEST_DB_VARS": os.path.join(self.ap, "test-db.vars"),
            "AP_TIME_SCALE": self.scale, "AP_NO_SYSTEMCTL": "1", "AP_HEARTBEAT_S": "1", "AP_BETWEEN_CYCLES_S": "1",
            "STUB_SCENARIO": self.scenario, "STUB_CALLS": self.calls,
        })
        e.update(extra)
        return e

    def scenario_is(self, steps):
        with open(self.scenario, "w") as fh:
            json.dump(steps, fh)

    def run_runner(self, max_cycles, timeout=60, **extra):
        return subprocess.run([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES=str(max_cycles), **extra),
                              capture_output=True, text=True, timeout=timeout)

    def events(self, kind=None):
        out = []
        try:
            with open(os.path.join(self.ap, "events.jsonl")) as fh:
                for line in fh:
                    rec = json.loads(line)
                    if kind is None or rec["kind"] == kind:
                        out.append(rec)
        except OSError:
            pass
        return out

    def ncalls(self):
        try:
            with open(self.calls) as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0


class Acceptance(RunnerHarness):
    def test_1_limit_with_reset_time_sleeps_until_reset_then_resumes(self):
        reset_local = (datetime.datetime.now(autopilot.LOCAL_TZ) + datetime.timedelta(minutes=10)).replace(second=0, microsecond=0)
        text = f"You've hit your session limit · resets {reset_local.strftime('%I:%M%p').lstrip('0').lower()}"
        self.scenario_is([{"kind": "limit", "text": text}, {"kind": "success"}])
        r = self.run_runner(2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ncalls(), 2, "resumed after the wait")
        nxt = self.events("next")
        self.assertEqual(nxt[0]["state"], "waiting-limit")
        wake = datetime.datetime.fromisoformat(nxt[0]["wake"])
        delta = (wake - reset_local.astimezone(datetime.timezone.utc)).total_seconds()
        self.assertTrue(120 <= delta <= 300, f"wake is reset + 2-5 min jitter, got {delta}")
        self.assertEqual(self.events("cycle-end")[1]["outcome"], "ok")

    def test_2_limit_without_reset_backs_off_20_40_60(self):
        self.scenario_is([{"kind": "limit", "text": "Claude AI usage limit reached"}] * 4 + [{"kind": "success"}])
        r = self.run_runner(5, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        waits = [round(e["wait_s"] / 60) for e in self.events("next") if e["state"] == "waiting-limit"]
        self.assertEqual(waits, [20, 40, 60, 60])
        self.assertEqual(self.ncalls(), 5)

    def test_3_crash_restarts_after_60s_and_caps_at_six(self):
        self.scenario_is([{"kind": "crash", "rc": 1}] * 6 + [{"kind": "success"}])
        r = self.run_runner(7, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        nxt = self.events("next")
        self.assertEqual([e["state"] for e in nxt[:6]], ["restarting"] * 5 + ["failure-cap"])
        self.assertTrue(all(55 <= e["wait_s"] <= 65 for e in nxt[:5]))
        self.assertTrue(7100 <= nxt[5]["wait_s"] <= 7300)
        self.assertEqual(len(self.events("failure-cap")), 1)
        self.assertEqual(self.ncalls(), 7)

    def test_4a_pause_holds_cycles_until_removed(self):
        self.scenario_is([{"kind": "success"}])
        open(os.path.join(self.ap, "PAUSE"), "w").close()
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        self.assertEqual(self.ncalls(), 0, "no cycle while paused")
        hb = read_json(os.path.join(self.ap, "heartbeat.json"))
        self.assertEqual(hb["state"], "paused")
        os.remove(os.path.join(self.ap, "PAUSE"))
        self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(self.ncalls(), 1)

    def test_4b_stop_finishes_the_current_cycle_then_exits_64(self):
        self.scenario_is([{"kind": "sleep", "seconds": 2}, {"kind": "success"}])
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        open(os.path.join(self.ap, "STOP"), "w").close()
        self.assertEqual(proc.wait(timeout=30), 64)
        ends = self.events("cycle-end")
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]["outcome"], "ok", "the running cycle was allowed to finish")
        self.assertEqual(read_json(os.path.join(self.ap, "state.json"))["state"], "stopped")

    def test_4c_pause_window_from_operator_settings(self):
        start = datetime.datetime.now(autopilot.LOCAL_TZ) - datetime.timedelta(minutes=5)
        end = start + datetime.timedelta(minutes=30)
        window = f'["{start:%H:%M}-{end:%H:%M} Asia/Kolkata"]'
        with open(os.path.join(self.ap, "MASTER_PROMPT.md"), "w") as fh:
            fh.write(MASTER.replace("AUTOPILOT_PAUSE_WINDOWS: []", f"AUTOPILOT_PAUSE_WINDOWS: {window}"))
        self.scenario_is([{"kind": "success"}])
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        self.assertEqual(self.ncalls(), 0)
        open(os.path.join(self.ap, "STOP"), "w").close()
        self.assertEqual(proc.wait(timeout=30), 64)

    def test_5_second_instance_is_refused(self):
        self.scenario_is([{"kind": "sleep", "seconds": 3}])
        first = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="1"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        second = self.run_runner(1, timeout=20)
        self.assertEqual(second.returncode, 3)
        self.assertIn("another instance", second.stderr)
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertEqual(self.ncalls(), 1)

    def test_auth_failure_is_reported_once_and_retried(self):
        self.scenario_is([{"kind": "auth"}, {"kind": "auth"}, {"kind": "success"}])
        r = self.run_runner(3, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        nxt = self.events("next")
        self.assertEqual([e["state"] for e in nxt[:2]], ["waiting-auth", "waiting-auth"])
        self.assertTrue(all(1790 <= e["wait_s"] <= 1810 for e in nxt[:2]))
        self.assertEqual(len(self.events("needs-human")), 1)
        self.assertIn("claude auth login", read(os.path.join(self.ap, "NEEDS_HUMAN.runtime.md")))

    def test_max_turns_is_a_normal_end_of_cycle(self):
        self.scenario_is([{"kind": "max_turns"}, {"kind": "success"}])
        r = self.run_runner(2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([e["outcome"] for e in self.events("cycle-end")], ["max_turns", "ok"])
        self.assertEqual(self.events("next")[0]["state"], "idle")

    def test_complete_status_disables_and_exits_64(self):
        docs = os.path.join(self.wt, "docs/ai-platform-upgrade")
        with open(os.path.join(docs, "RESUME.md"), "w") as fh:
            fh.write("STATUS: COMPLETE\n")
        with open(os.path.join(docs, "FINAL_REPORT.md"), "w") as fh:
            fh.write("# Final report\n")
        self.scenario_is([{"kind": "success"}])
        r = self.run_runner(1)
        self.assertEqual(r.returncode, 64)
        self.assertEqual(self.ncalls(), 0)
        self.assertEqual(len(self.events("disable-service-skipped")), 1)

    def test_cycle_invocation_carries_the_guardrails(self):
        self.scenario_is([{"kind": "success"}])
        self.run_runner(1)
        call = json.loads(read(self.calls).splitlines()[0])
        args = call["args"]
        for flag, value in (("--permission-mode", "auto"), ("--permission-prompts", "none"), ("--max-turns", "150"), ("--output-format", "stream-json")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args[args.index("--settings") + 1], os.path.join(self.ap, "settings.json"))
        self.assertEqual(args[args.index("-p") + 1], "cycle prompt")
        self.assertEqual(call["cycle"], "1")
        self.assertEqual(call["test_db"], "192.0.2.20:15432", "test database variables reach the cycle")
        hb = read_json(os.path.join(self.ap, "heartbeat.json"))
        self.assertIn("A-01", hb["current_task"] or "")


class AcceptanceFixes(RunnerHarness):
    def test_interrupted_cycle_is_not_recorded_as_success(self):
        # R2: a cycle killed mid-work (SIGTERM) is 'interrupted', not 'ok'.
        self.scenario_is([{"kind": "multi_results_then_sleep", "turns": [45, 1], "seconds": 30}])
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="1"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        proc.terminate()
        proc.wait(timeout=30)
        rec = read_json(os.path.join(self.ap, "state.json"))["last_cycle"]
        self.assertEqual(rec["outcome"], "interrupted")
        self.assertEqual(rec["num_turns"], 46, "num_turns is summed across result events")
        self.assertGreaterEqual(rec["result_count"], 2)

    def test_non_object_json_line_does_not_crash_the_runner(self):
        # R12: a bare list/number line is skipped; the cycle still completes.
        self.scenario_is([{"kind": "badjson"}])
        r = self.run_runner(1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.events("cycle-end")[0]["outcome"], "ok")

    def test_test_db_vars_only_pass_the_allowed_keys(self):
        # R1: PATH/NODE_OPTIONS in test-db.vars never reach the cycle environment.
        with open(os.path.join(self.ap, "test-db.vars"), "w") as fh:
            fh.write("TEST_DATABASE_ALLOWED_HOSTS=192.0.2.20:15432\nPATH=/tmp/evil:/usr/bin\nNODE_OPTIONS=--require=/tmp/x.js\n")
        self.scenario_is([{"kind": "success"}])
        self.run_runner(1)
        self.assertEqual(len(self.events("test-db-vars-ignored")), 1)
        self.assertEqual(set(self.events("test-db-vars-ignored")[0]["keys"]), {"PATH", "NODE_OPTIONS"})

    def test_pending_limit_wait_survives_restart(self):
        # R3: a usage-limit wait is kept in state.json across a SIGTERM + restart.
        reset = datetime.datetime.now(autopilot.LOCAL_TZ) + datetime.timedelta(minutes=30)
        text = f"You've hit your session limit · resets {reset.strftime('%I:%M%p').lstrip('0').lower()}"
        self.scenario_is([{"kind": "limit", "text": text}, {"kind": "success"}])
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="2"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # wait for the limit wait to be scheduled
        for _ in range(200):
            st = self._state()
            if st.get("state") == "waiting-limit" and st.get("next_wake"):
                break
            time.sleep(0.05)
        self.assertEqual(self._state()["state"], "waiting-limit")
        wake_before = self._state()["next_wake"]
        self.assertIsNotNone(wake_before)
        proc.terminate()
        proc.wait(timeout=30)
        after = self._state()
        self.assertEqual(after["state"], "stopped")
        self.assertEqual(after["next_wake"], wake_before, "the pending wake is kept across the stop (R3)")
        self.assertEqual(self.ncalls(), 1, "the second cycle did not start before the reset")

    def test_pause_keeps_the_pending_wake(self):
        # R3: PAUSE during a limit wait does not erase next_wake.
        reset = datetime.datetime.now(autopilot.LOCAL_TZ) + datetime.timedelta(minutes=30)
        text = f"You've hit your session limit · resets {reset.strftime('%I:%M%p').lstrip('0').lower()}"
        self.scenario_is([{"kind": "limit", "text": text}, {"kind": "success"}])
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(AP_MAX_CYCLES="2"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            if self._state().get("state") == "waiting-limit" and self._state().get("next_wake"):
                break
            time.sleep(0.05)
        wake_before = self._state()["next_wake"]
        open(os.path.join(self.ap, "PAUSE"), "w").close()
        time.sleep(1.0)
        paused = self._state()
        self.assertEqual(paused["state"], "paused")
        self.assertEqual(paused["next_wake"], wake_before, "the wake time survives the pause (R3)")
        open(os.path.join(self.ap, "STOP"), "w").close()
        os.remove(os.path.join(self.ap, "PAUSE"))
        proc.wait(timeout=30)
        self.assertEqual(self.ncalls(), 1)

    def test_max_days_checkpoint_waits_for_pause(self):
        # R5: at MAX_AUTONOMOUS_DAYS the final checkpoint still honours PAUSE.
        self.scenario_is([{"kind": "success"}])
        open(os.path.join(self.ap, "PAUSE"), "w").close()
        state = {"started_at": autopilot.iso(autopilot.now() - datetime.timedelta(days=20)), "cycle": 5}
        with open(os.path.join(self.ap, "state.json"), "w") as fh:
            json.dump(state, fh)
        proc = subprocess.Popen([sys.executable, "-I", RUNNER], env=self.env(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        self.assertEqual(self.ncalls(), 0, "no checkpoint cycle starts while paused")
        self.assertEqual(self._state()["state"], "paused")
        self.assertEqual(len(self.events("max-days-reached")), 0)
        open(os.path.join(self.ap, "STOP"), "w").close()
        os.remove(os.path.join(self.ap, "PAUSE"))
        self.assertEqual(proc.wait(timeout=30), 64)

    def test_max_days_writes_checkpoint_then_expires(self):
        # R5: with no gate blocking, the checkpoint runs once and the runner expires.
        self.scenario_is([{"kind": "success"}])
        state = {"started_at": autopilot.iso(autopilot.now() - datetime.timedelta(days=20)), "cycle": 5}
        with open(os.path.join(self.ap, "state.json"), "w") as fh:
            json.dump(state, fh)
        r = self.run_runner(0)  # MAX_CYCLES unset: the loop runs until it expires
        self.assertEqual(r.returncode, 64, r.stderr)
        self.assertEqual(len(self.events("max-days-reached")), 1)
        self.assertEqual(self.ncalls(), 1, "exactly one final-checkpoint cycle")
        self.assertTrue(self._state().get("final_checkpoint_done"))
        self.assertEqual(self._state()["state"], "expired")

    def test_max_days_retries_checkpoint_on_a_usage_limit(self):
        # R5: a limited account does not lose its final checkpoint.
        reset = datetime.datetime.now(autopilot.LOCAL_TZ) + datetime.timedelta(minutes=5)
        text = f"resets {reset.strftime('%I:%M%p').lstrip('0').lower()}"
        self.scenario_is([{"kind": "limit", "text": text}, {"kind": "success"}])
        state = {"started_at": autopilot.iso(autopilot.now() - datetime.timedelta(days=20)), "cycle": 5}
        with open(os.path.join(self.ap, "state.json"), "w") as fh:
            json.dump(state, fh)
        r = self.run_runner(0, timeout=90)
        self.assertEqual(r.returncode, 64, r.stderr)
        self.assertEqual(self.ncalls(), 2, "the checkpoint was retried after the limit reset")
        self.assertTrue(self._state().get("final_checkpoint_done"))

    def _state(self):
        try:
            return read_json(os.path.join(self.ap, "state.json"))
        except (OSError, ValueError):
            return {}


class Units(unittest.TestCase):
    def test_reset_parser_formats(self):
        at = datetime.datetime(2026, 10, 3, 6, 0, tzinfo=datetime.timezone.utc)  # 11:30 IST, Saturday
        ist = autopilot.LOCAL_TZ
        cases = {
            "You've hit your session limit · resets 3:45pm": datetime.datetime(2026, 10, 3, 15, 45, tzinfo=ist),
            "You've hit your weekly limit · resets Mon 12:00am": datetime.datetime(2026, 10, 5, 0, 0, tzinfo=ist),
            "limit · resets 9am": datetime.datetime(2026, 10, 4, 9, 0, tzinfo=ist),
            "resets Oct 7, 10am": datetime.datetime(2026, 10, 7, 10, 0, tzinfo=ist),
            "resets 3pm (UTC)": datetime.datetime(2026, 10, 3, 15, 0, tzinfo=datetime.timezone.utc),
            "resets in 2h 15m": at + datetime.timedelta(hours=2, minutes=15),
            "Claude AI usage limit reached|1759500000": datetime.datetime.fromtimestamp(1759500000, datetime.timezone.utc),
            # R10: trailing text after the timezone, and 'tomorrow at'
            "You've hit your session limit · resets 3pm (Asia/Kolkata) · /upgrade to keep using Claude Code":
                datetime.datetime(2026, 10, 3, 15, 0, tzinfo=ist),
            "limit reached. resets tomorrow at 9am": datetime.datetime(2026, 10, 4, 9, 0, tzinfo=ist),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(autopilot.parse_reset(text, at), want.astimezone(datetime.timezone.utc))
        self.assertIsNone(autopilot.parse_reset("Claude AI usage limit reached", at))

    def test_reset_just_passed_clock_is_now_not_tomorrow(self):
        # R10(a): 'resets 3pm' seen a minute after 3pm means now, not +24h.
        at = datetime.datetime(2026, 10, 3, 9, 31, tzinfo=datetime.timezone.utc)  # 15:01 IST
        got = autopilot.parse_reset("You've hit your session limit · resets 3pm", at)
        self.assertLess((got - at).total_seconds(), 120, "a just-passed clock time resets ~now")
        # A clock time passed hours ago still means the next day.
        morning = datetime.datetime(2026, 10, 3, 6, 0, tzinfo=datetime.timezone.utc)  # 11:30 IST
        got2 = autopilot.parse_reset("limit · resets 9am", morning)
        self.assertEqual(got2, datetime.datetime(2026, 10, 4, 9, 0, tzinfo=autopilot.LOCAL_TZ).astimezone(datetime.timezone.utc))

    def test_rate_event_reset_is_preferred(self):
        epoch = 1759500000
        got = autopilot.rate_event_reset([{"status": "allowed"}, {"status": "rejected", "resetsAt": epoch}])
        self.assertEqual(got, datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc))
        self.assertIsNone(autopilot.rate_event_reset([{"status": "allowed", "resetsAt": epoch}]))

    def test_pause_windows(self):
        at = datetime.datetime(2026, 10, 3, 6, 0, tzinfo=datetime.timezone.utc)  # 11:30 IST
        self.assertTrue(autopilot.in_pause_window(["10:00-18:00 Asia/Kolkata"], at))
        self.assertFalse(autopilot.in_pause_window(["12:00-18:00 Asia/Kolkata"], at))
        self.assertTrue(autopilot.in_pause_window(["22:00-12:00 Asia/Kolkata"], at), "overnight window")
        self.assertFalse(autopilot.in_pause_window([], at))

    def test_classification(self):
        c = autopilot.classify
        ok = {"subtype": "success", "is_error": False}
        self.assertEqual(c(0, ok, [], [])[0], "ok")
        self.assertEqual(c(1, {"subtype": "error_max_turns", "is_error": True}, [], [])[0], "max_turns")
        self.assertEqual(c(124, None, [], [])[0], "timeout")
        self.assertEqual(c(1, None, ["x"], [{"error_type": "rate_limit"}])[0], "usage_limit")
        self.assertEqual(c(1, None, ["x"], [{"error_type": "overloaded"}])[0], "transient")
        self.assertEqual(c(1, {"subtype": "success", "is_error": True, "result": "API Error: 529 Overloaded"}, [], [])[0], "transient")
        self.assertEqual(c(1, None, [], [{"error_type": "authentication_failed"}])[0], "auth")
        self.assertEqual(c(1, None, [], [{"error_type": "model_not_found"}])[0], "config")
        self.assertEqual(c(1, None, ["boom"], [])[0], "crash")

    def test_classification_does_not_trust_assistant_prose(self):
        # R4: a max-turns / timeout / crash whose text merely mentions a limit
        # must not become usage_limit; the runner never feeds prose to classify,
        # and even if it did, structured signals and max-turns/timeout win.
        c = autopilot.classify
        self.assertEqual(c(1, {"subtype": "error_max_turns", "is_error": True}, ["I added a per-key rate limit and a 429 test."], [])[0], "max_turns")
        self.assertEqual(c(124, None, ["Wrote the rate limiter; the bucket resets at 9am."], [])[0], "timeout")
        # A real limit message (final result, is_error) still classifies as usage_limit.
        self.assertEqual(c(1, {"subtype": "success", "is_error": True, "result": "You've hit your session limit · resets 3pm (Asia/Kolkata)"}, [], [])[0], "usage_limit")
        self.assertEqual(c(1, None, [], [], rate_events=[{"status": "rejected", "resetsAt": 1759500000}])[0], "usage_limit")

    def test_classification_interrupted(self):
        # R2: a cycle cut off by a service restart is not a clean success.
        c = autopilot.classify
        ok = {"subtype": "success", "is_error": False}
        self.assertEqual(c(143, ok, [], [])[0], "interrupted")
        self.assertEqual(c(-15, ok, [], [])[0], "interrupted")
        self.assertEqual(c(0, ok, [], [], stop_signal=True)[0], "interrupted")
        self.assertEqual(c(0, ok, [], [])[0], "ok", "a clean rc 0 success is still ok")

    def test_classification_ignores_subagent_or_recovered_failures(self):
        # R8: a final success outranks an earlier StopFailure; subagent/other-session
        # records are filtered out before classify sees them (see finish_cycle).
        c = autopilot.classify
        ok = {"subtype": "success", "is_error": False}
        self.assertEqual(c(0, ok, [], [{"error_type": "rate_limit"}])[0], "ok")
        self.assertEqual(c(1, None, [], [{"error_type": "rate_limit"}])[0], "usage_limit")

    def test_redaction(self):
        with tempfile.NamedTemporaryFile("w", suffix=".vars", delete=False) as fh:
            fh.write("API_KEY=" + "abcdefghij123456XYZ\n")
            path = fh.name
        try:
            red = autopilot.Redactor([path])
            out = red('{"x": "abcdefghij123456XYZ", "url": "postgresql://u:' + 'longpassword1@h/db", "password": "hunter22xyz"}')
            self.assertNotIn("abcdefghij123456XYZ", out)
            self.assertNotIn("longpassword1", out)
            self.assertNotIn("hunter22xyz", out)
        finally:
            os.remove(path)

    def test_redaction_keeps_json_parseable(self):
        # R6: numbers and structure survive; the value class does not eat braces.
        red = autopilot.Redactor([])
        for line in (
            '{"usage":{"cache_read_input_tokens":123456,"output_tokens":3}}',
            '{"usage":{"output_tokens":12345}}',
            '{"type":"result","total_cost_usd":0.21,"num_turns":45}',
            '{"a":[1,2,3],"b":null,"c":true}',
        ):
            with self.subTest(line=line):
                out = red(line)
                json.loads(out)  # must still parse
        d = json.loads(red('{"usage":{"cache_read_input_tokens":123456,"output_tokens":3}}'))
        self.assertEqual(d["usage"]["cache_read_input_tokens"], 123456, "token counts are not redacted")

    def test_redaction_json_secret_leaves(self):
        # R6/R7: secret-shaped and secret-named leaves are redacted, structure kept.
        red = autopilot.Redactor([])
        out = red('{"headers":{"Authorization":"Bearer ' + "0000000000aaaaaaaaaa" + '"},"key":"tsk_test_0123456789abcdef_' + "A" * 20 + '","n":7}')
        d = json.loads(out)
        self.assertEqual(d["n"], 7)
        self.assertNotIn("0000000000aaaaaaaaaa", out)
        self.assertNotIn("tsk_test_0123456789abcdef", out)
        # Escaped inner JSON inside a tool_result string is scrubbed too.
        inner = json.dumps({"password": "s3cretvalue99"})
        out2 = red(json.dumps({"type": "user", "content": inner}))
        self.assertNotIn("s3cretvalue99", out2)
        json.loads(out2)

    def test_redaction_plain_text_forms(self):
        # R7: bearer tokens, platform keys and CLI flags in non-JSON text.
        red = autopilot.Redactor([])
        for text, secret in (
            ('curl -H "Authorization: Bearer ' + "0000000000aaaaaaaaaa" + '"', "0000000000aaaaaaaaaa"),
            ("mysql --password hunterpass22", "hunterpass22"),
            ("--token=abcd1234efgh", "abcd1234efgh"),
            ("key tsk_live_0123456789abcdef_" + "B" * 20, "tsk_live_0123456789abcdef"),
        ):
            with self.subTest(text=text):
                self.assertNotIn(secret, red(text))

    def test_load_vars_allowlist(self):
        # R1: only the test-database keys are read from test-db.vars.
        with tempfile.NamedTemporaryFile("w", suffix=".vars", delete=False) as fh:
            fh.write("TEST_DATABASE_URL=postgresql://t:t@h:15432/x_test\n")
            fh.write("TEST_DATABASE_ALLOWED_HOSTS=h:15432\n")
            fh.write("PATH=/tmp/evil:/usr/bin\n")
            fh.write("NODE_OPTIONS=--require=/tmp/x.js\n")
            fh.write("export ANTHROPIC_BASE_URL=http://evil\n")
            path = fh.name
        try:
            out, dropped = autopilot.load_vars(path, allowed=autopilot.TEST_DB_ALLOWED_KEYS)
            self.assertEqual(set(out), {"TEST_DATABASE_URL", "TEST_DATABASE_ALLOWED_HOSTS"})
            self.assertEqual(set(dropped), {"PATH", "NODE_OPTIONS", "ANTHROPIC_BASE_URL"})
            allv, _ = autopilot.load_vars(path)
            self.assertIn("PATH", allv, "without an allowlist nothing is dropped")
        finally:
            os.remove(path)

    def test_tools_resolve_to_absolute_paths(self):
        for tool in (autopilot.NICE, autopilot.IONICE, autopilot.TIMEOUT):
            self.assertTrue(os.path.isabs(tool), tool)

    def test_operator_settings_parse(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("x\n```yaml\nDEV_BRANCH: autopilot/dev   # comment\nMAX_AUTONOMOUS_DAYS: 14\nAUTOPILOT_PAUSE_WINDOWS: [\"10:00-18:00 Asia/Kolkata\"]\nMAX_PARALLEL_SUBAGENTS: unlimited\n```\n")
            path = fh.name
        try:
            s = autopilot.operator_settings(path)
            self.assertEqual(s["DEV_BRANCH"], "autopilot/dev")
            self.assertEqual(s["MAX_AUTONOMOUS_DAYS"], 14)
            self.assertEqual(s["AUTOPILOT_PAUSE_WINDOWS"], ["10:00-18:00 Asia/Kolkata"])
            self.assertEqual(s["MAX_PARALLEL_SUBAGENTS"], "unlimited")
        finally:
            os.remove(path)

    def test_operator_settings_quote_and_block_lists(self):
        # R11: single-quoted flow lists and block lists parse; a bad value warns.
        def windows(body):
            with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
                fh.write("x\n```yaml\n" + body + "\n```\n")
                path = fh.name
            warnings = []
            try:
                s = autopilot.operator_settings(path, warn=lambda k, m: warnings.append((k, m)))
            finally:
                os.remove(path)
            return s["AUTOPILOT_PAUSE_WINDOWS"], warnings
        got, warns = windows("AUTOPILOT_PAUSE_WINDOWS: ['10:00-18:00 Asia/Kolkata']")
        self.assertEqual(got, ["10:00-18:00 Asia/Kolkata"])
        self.assertEqual(warns, [])
        got, warns = windows("AUTOPILOT_PAUSE_WINDOWS:\n  - \"10:00-18:00 Asia/Kolkata\"\n  - '22:00-23:00 UTC'")
        self.assertEqual(got, ["10:00-18:00 Asia/Kolkata", "22:00-23:00 UTC"])
        self.assertEqual(warns, [])
        got, warns = windows("AUTOPILOT_PAUSE_WINDOWS: not-a-list")
        self.assertEqual(got, [], "a scalar is not a window list")
        got, warns = windows("AUTOPILOT_PAUSE_WINDOWS: [\"notatime\"]")
        self.assertTrue(any(k == "AUTOPILOT_PAUSE_WINDOWS" for k, _ in warns), "a malformed window is reported")


if __name__ == "__main__":
    unittest.main()
