"""Tests for runner.py — scenario execution, recovery, reporting."""

from c64_test_harness.runner import TestRunner, TestStatus


class TestTestRunner:
    def test_passing_scenario(self):
        runner = TestRunner()
        runner.add_scenario("pass", lambda: (True, "ok"))
        results = runner.run_all()
        assert len(results) == 1
        assert results[0].status == TestStatus.PASS
        assert results[0].message == "ok"
        assert runner.all_passed
        assert runner.exit_code == 0

    def test_failing_scenario(self):
        runner = TestRunner()
        runner.add_scenario("fail", lambda: (False, "bad"))
        results = runner.run_all()
        assert results[0].status == TestStatus.FAIL
        assert not runner.all_passed
        assert runner.exit_code == 1

    def test_error_scenario(self):
        def raise_error():
            raise RuntimeError("boom")

        runner = TestRunner()
        runner.add_scenario("error", raise_error)
        results = runner.run_all()
        assert results[0].status == TestStatus.ERROR
        assert "RuntimeError: boom" in results[0].message

    def test_multiple_scenarios(self):
        runner = TestRunner()
        runner.add_scenario("a", lambda: (True, "ok"))
        runner.add_scenario("b", lambda: (False, "nope"))
        runner.add_scenario("c", lambda: (True, "ok"))
        results = runner.run_all()
        assert len(results) == 3
        assert results[0].status == TestStatus.PASS
        assert results[1].status == TestStatus.FAIL
        assert results[2].status == TestStatus.PASS
        assert not runner.all_passed

    def test_recovery_called_on_fail(self):
        recovered = []

        def recovery():
            recovered.append(True)
            return True

        runner = TestRunner()
        runner.add_scenario("fail", lambda: (False, "bad"), recovery)
        runner.run_all()
        assert len(recovered) == 1

    def test_recovery_called_on_error(self):
        recovered = []

        def recovery():
            recovered.append(True)
            return True

        runner = TestRunner()
        runner.add_scenario("error", lambda: (_ for _ in ()).throw(ValueError("x")), recovery)
        runner.run_all()
        assert len(recovered) == 1

    def test_recovery_not_called_on_pass(self):
        recovered = []

        def recovery():
            recovered.append(True)
            return True

        runner = TestRunner()
        runner.add_scenario("pass", lambda: (True, "ok"), recovery)
        runner.run_all()
        assert len(recovered) == 0

    def test_duration_recorded(self):
        import time

        def slow_test():
            time.sleep(0.1)
            return True, "ok"

        runner = TestRunner()
        runner.add_scenario("slow", slow_test)
        results = runner.run_all()
        assert results[0].duration >= 0.05

    def test_print_summary(self, capsys):
        runner = TestRunner()
        runner.add_scenario("pass", lambda: (True, "ok"))
        runner.add_scenario("fail", lambda: (False, "nope"))
        runner.run_all()
        runner.print_summary()
        captured = capsys.readouterr()
        assert "RESULTS" in captured.out
        assert "[+] pass" in captured.out
        assert "[-] fail" in captured.out
        assert "1/2 passed" in captured.out

    def test_results_property(self):
        runner = TestRunner()
        runner.add_scenario("a", lambda: (True, "ok"))
        runner.run_all()
        # Returns a copy
        assert runner.results is not runner._results
        assert len(runner.results) == 1
