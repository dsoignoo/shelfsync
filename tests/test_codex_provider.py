import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_provider as codex


class CodexBoundaryTests(unittest.TestCase):
    def test_subprocess_environment_excludes_secrets_and_provider_overrides(self):
        with patch.dict(os.environ, {
            "HOME": "/login-home", "PATH": "/bin", "CODEX_HOME": "/cached-auth",
            "OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret",
            "OPENAI_BASE_URL": "https://untrusted.invalid", "CODEX_CONFIG": "hostile",
            "CHATGPT_BASE_URL": "https://untrusted.invalid", "NODE_OPTIONS": "--require=evil",
            "ASSEMBLYAI_API_KEY": "secret", "AUDIOBOOKSHELF_TOKEN": "secret",
        }, clear=True):
            self.assertEqual(codex._environment(), {
                "HOME": "/login-home", "PATH": "/bin", "CODEX_HOME": "/cached-auth",
            })



class CodexLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lock_patch = patch.object(codex, "_request_lock", asyncio.Lock())
        self.lock_patch.start()
        self.env_patch = patch.dict(os.environ, {"CODEX_TIMEOUT_SECONDS": "10", "CODEX_MODEL": ""})
        self.env_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.addCleanup(self.env_patch.stop)

    async def wait_for_file(self, path):
        async with asyncio.timeout(5):
            while not path.exists():
                await asyncio.sleep(0.01)

    async def assert_child_reaped(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    @unittest.skipUnless(os.name == "posix", "POSIX process lifecycle")
    async def test_cancellation_kills_and_reaps_running_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            pidfile = cwd / "pid"
            code = "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
            task = asyncio.create_task(codex._run_process(
                [sys.executable, "-c", code], cwd=cwd, env=os.environ.copy(), prompt=b"private source",
            ))
            await self.wait_for_file(pidfile)
            pid = int(pidfile.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await self.assert_child_reaped(pid)

    @unittest.skipUnless(os.name == "posix", "POSIX process lifecycle")
    async def test_cancellation_during_spawn_still_reaps_child(self):
        spawned = asyncio.Event()
        release_spawn = asyncio.Event()
        processes = []
        create = asyncio.create_subprocess_exec

        async def delayed_spawn(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release_spawn.wait()
            return process

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex.asyncio, "create_subprocess_exec", delayed_spawn):
                task = asyncio.create_task(codex._run_process(
                    [sys.executable, "-c", "import time;time.sleep(60)"],
                    cwd=Path(directory), env=os.environ.copy(), prompt=b"private",
                ))
                await asyncio.wait_for(spawned.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
                # A second cancellation must not interrupt the shielded cleanup.
                task.cancel()
                release_spawn.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(len(processes), 1)
        await self.assert_child_reaped(processes[0].pid)

    async def test_cancellation_during_normal_teardown_is_not_swallowed(self):
        cleaning = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup = codex._kill_and_reap

        async def delayed_cleanup(process):
            cleaning.set()
            await release_cleanup.wait()
            await cleanup(process)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(codex, "_kill_and_reap", delayed_cleanup):
                task = asyncio.create_task(codex._run_process(
                    [sys.executable, "-c", "pass"], cwd=Path(directory),
                    env=os.environ.copy(), prompt=b"",
                ))
                await asyncio.wait_for(cleaning.wait(), 5)
                task.cancel()
                release_cleanup.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    @unittest.skipUnless(os.name == "posix", "POSIX process lifecycle")
    async def test_deadline_kills_real_process_and_removes_request_directory(self):
        directories = []
        pids = []

        def sleeping_command(executable, instructions, output, image):
            directories.append(output.parent)
            code = "import os,pathlib,time; pathlib.Path('pid').write_text(str(os.getpid())); time.sleep(60)"
            return [sys.executable, "-c", code]

        with patch.object(codex.shutil, "which", return_value=sys.executable), \
             patch.object(codex, "_command", sleeping_command), \
             patch.dict(os.environ, {"CODEX_TIMEOUT_SECONDS": "1"}):
            task = asyncio.create_task(codex.generate_text("Summarize", "private source"))
            async with asyncio.timeout(5):
                while not directories:
                    await asyncio.sleep(0.01)
                await self.wait_for_file(directories[0] / "pid")
                pids.append(int((directories[0] / "pid").read_text()))
            with self.assertRaises(TimeoutError):
                await task
        self.assertFalse(directories[0].exists())
        await self.assert_child_reaped(pids[0])

    async def test_empty_final_output_fails_and_removes_image_and_output(self):
        directories = []
        source = "Private book passage not permitted in argv"
        image = b"\x89PNG\r\n\x1a\nimage data"

        async def empty_result(command, *, cwd, env, prompt):
            directories.append(cwd)
            self.assertNotIn(source, " ".join(command))
            self.assertIn(source.encode(), prompt)
            self.assertEqual((cwd / "source.png").read_bytes(), image)
            self.assertFalse((cwd / "answer.txt").exists())
            (cwd / "answer.txt").write_text("  \n")
            return 0

        with patch.object(codex.shutil, "which", return_value="/bin/codex"), \
             patch.object(codex, "_run_process", empty_result):
            with self.assertRaises(codex.CodexError):
                await codex.generate_text("Summarize", source, image=image)
        self.assertFalse(directories[0].exists())

    async def test_failed_exit_never_returns_partial_output(self):
        directories = []

        async def failed_result(command, *, cwd, env, prompt):
            directories.append(cwd)
            (cwd / "answer.txt").write_text("private incomplete output")
            return 1

        with patch.object(codex.shutil, "which", return_value="/bin/codex"), \
             patch.object(codex, "_run_process", failed_result):
            with self.assertRaises(codex.CodexError) as error:
                await codex.generate_text("Summarize", "private book")
        self.assertNotIn("private", str(error.exception))
        self.assertFalse(directories[0].exists())

    async def test_queued_deadline_does_not_spawn_another_cli(self):
        async with codex._request_lock:
            with patch.object(codex.shutil, "which", return_value="/bin/codex"), \
                 patch.object(codex, "_run_process") as run, \
                 patch.dict(os.environ, {"CODEX_TIMEOUT_SECONDS": "0.01"}):
                with self.assertRaises(TimeoutError):
                    await codex.generate_text("Summarize", "queued source")
                run.assert_not_called()
        self.assertFalse(codex._request_lock.locked())

    async def test_invalid_timeout_and_image_suffix_fail_before_spawning(self):
        for timeout in ("nan", "inf", "0", "-1", "not-a-number"):
            with self.subTest(timeout=timeout), \
                 patch.dict(os.environ, {"CODEX_TIMEOUT_SECONDS": timeout}), \
                 patch.object(codex, "_run_process") as run:
                with self.assertRaises(codex.CodexError):
                    await codex.generate_text("Summarize", "private source")
                run.assert_not_called()
        with patch.object(codex, "_run_process") as run:
            with self.assertRaises(codex.CodexError):
                await codex.generate_text("Read image", "", image=b"image", image_suffix="/../../auth.json")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
