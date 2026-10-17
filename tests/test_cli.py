"""The command line: every command and option parses and behaves, streams and exit codes stay apart."""

import argparse
import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mirror  # noqa: E402
import test_mirror as tm  # noqa: E402


class CliTest(tm.CliMixin, tm.FakeRegistry):
    def test_progress_is_on_stderr_and_data_on_stdout(self):
        make = tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("publish", "--dry-run")
        self.assertEqual((code, out), (0, f"would upload {make.name}\n"))
        self.assertIn("1 file(s): 1 to upload", self.err)
        code, out = self.run_cli("publish")
        self.assertEqual((code, out), (0, ""), "publish prints progress, not data")
        self.assertIn(f"uploaded {make.name}", self.err)

    def test_quiet_silences_progress_but_not_errors_or_data(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("-q", "publish", "--dry-run")
        self.assertEqual((code, out), (0, "would upload idna-3.20-py3-none-any.whl\n"))
        self.assertEqual(self.err, "")
        code, _ = self.run_cli("--quiet", "publish")
        self.assertEqual((code, self.err), (0, ""))
        with mock.patch.dict(os.environ, {"CI_JOB_TOKEN": "wrong"}):
            code, _ = self.run_cli("-q", "publish")
        self.assertEqual(code, 1)
        self.assertRegex(self.err, rf"^{mirror.prog()}: ")

    def test_runtime_errors_and_warnings_carry_the_program_name(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            code, out = self.run_cli("publish")
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(self.err, rf"^{mirror.prog()}: registry settings missing")
        self.assertNotIn("ERROR", self.err)
        wheelhouse = self.target("linux-py3.12")
        (wheelhouse / "pkg-0.1.tar.gz").write_bytes(b"x")
        self.run_cli("publish", "--dry-run")
        self.assertIn(f"{mirror.prog()}: warning: ignoring wheelhouse/pkg-0.1.tar.gz", self.err)
        self.assertEqual(mirror.prog(), Path(sys.argv[0]).name)

    def test_version_prints_the_program_and_its_version_on_stdout(self):
        code, out = self.run_cli("--version")
        self.assertEqual((code, out, self.err), (0, f"{mirror.prog()} {mirror.VERSION}\n", ""))
        self.assertEqual(mirror.VERSION, "0.1.0")
        self.assertRegex(mirror.VERSION, r"^\d+\.\d+\.\d+$")

    def test_help_names_the_program_as_started_and_never_a_fixed_script_name(self):
        with mock.patch.object(sys, "argv", ["pypi-mirror"]):
            commands = [action for action in mirror.build_parser()._actions if hasattr(action, "choices") and action.choices]
            names = list(commands[0].choices)
            for argv in ([], *([name] for name in names)):
                with self.subTest(argv=argv):
                    code, out = self.run_cli(*argv, "-h")
                    self.assertEqual((code, self.err), (0, ""))
                    self.assertIn("pypi-mirror", out)
                    self.assertNotIn("mirror.py", out)
                    self.assertNotIn("%(prog)s", out)
                    self.assertNotIn("{prog}", out)

    def test_every_command_has_a_description_and_an_example(self):
        with mock.patch.object(sys, "argv", ["pypi-mirror"]):
            names = list(mirror.build_parser()._subparsers._group_actions[0].choices)
            for name in names:
                code, out = self.run_cli(name, "-h")
                self.assertIn("examples:", out, name)
                self.assertIn(f"pypi-mirror {name}", out, name)

    def test_usage_errors_exit_2_and_runtime_failures_exit_1(self):
        self.target("linux-py3.12")
        cases = {
            "no command": ([], 2),
            "unknown command": (["bogus"], 2),
            "unknown option": (["publish", "--bogus"], 2),
            "export with neither a date nor a package": (["export"], 2),
            "malformed date": (["export", "--since", "09/15/2026"], 2),
            "malformed package spec": (["export", "bad spec!"], 2),
            "import with no source": (["import"], 2),
            "post without a bundle": (["publish", "--nifi-url", "http://127.0.0.1:1/x"], 2),
            "no such target": (["download", "linux-py3.99"], 1),
        }
        for label, (argv, expected) in cases.items():
            with self.subTest(label):
                code, out = self.run_cli(*argv)
                self.assertEqual(code, expected, self.err)
                self.assertEqual(out, "")
                self.assertRegex(self.err, rf"^(usage: |{mirror.prog()}: )" if expected == 2 else rf"^{mirror.prog()}: ")
        self.assertEqual(self.run_cli("list")[0], 0)

    def test_a_post_without_a_bundle_is_a_usage_error_and_uploads_nothing(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        code, out = self.run_cli("publish", "--nifi-url", "http://127.0.0.1:1/x")
        self.assertEqual(code, 2)
        self.assertIn("-t", self.err)
        self.assertEqual(tm.FakeGitLab.files, {})

    def test_a_malformed_environment_date_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"MIRROR_EXPORT_SINCE": "yesterday"}):
            self.assertEqual(self.run_cli("export")[0], 2)

    def test_manylinux_is_checked_before_any_work(self):
        self.target("linux-py3.12")
        for bad in ("2_1", "3_0", "x"):
            with self.subTest(bad), mock.patch.dict(os.environ, {"MIRROR_MANYLINUX": bad}):
                code, out = self.run_cli("check")
                self.assertEqual(code, 2)
                self.assertIn("MIRROR_MANYLINUX", self.err)
        with mock.patch.dict(os.environ, {"MIRROR_MANYLINUX": "2_17"}):
            self.assertEqual(self.run_cli("list")[0], 0)

    def test_boolean_environment_variables_take_the_coreutils_spellings_in_any_case(self):
        for word in ("1", "yes", "y", "true", "t", "on"):
            for spelled in (word, word.upper(), f" {word.title()} "):
                with self.subTest(spelled), mock.patch.dict(os.environ, {"MIRROR_BUNDLE_GIT": spelled}):
                    self.assertTrue(mirror.env_flag("MIRROR_BUNDLE_GIT"))
        for word in ("0", "no", "n", "false", "f", "off"):
            for spelled in (word, word.upper()):
                with self.subTest(spelled), mock.patch.dict(os.environ, {"MIRROR_BUNDLE_REQUIREMENTS": spelled}):
                    self.assertFalse(mirror.env_flag("MIRROR_BUNDLE_REQUIREMENTS", True))
        self.assertTrue(mirror.env_flag("UNSET_FLAG", True))
        with mock.patch.dict(os.environ, {"EMPTY_FLAG": ""}):
            self.assertFalse(mirror.env_flag("EMPTY_FLAG"))

    def test_a_value_that_is_not_a_boolean_is_a_usage_error(self):
        make_dir = tm.make_wheel(self.target("linux-py3.12", "idna\n"), "idna-3.20-py3-none-any.whl")
        for name in ("MIRROR_BUNDLE_GIT", "MIRROR_BUNDLE_REQUIREMENTS"):
            with self.subTest(name), mock.patch.dict(os.environ, {name: "maybe"}):
                code, out = self.run_cli("publish", "-t", str(Path(self.tmp.name) / "d"))
                self.assertEqual(code, 2)
                self.assertIn(f"{name}='maybe' is not a boolean", self.err)
        self.assertTrue(make_dir.exists())
        self.assertEqual(tm.FakeGitLab.files, {}, "nothing uploads before the setting is refused")

    def test_bundle_extras_follow_the_environment_at_call_time(self):
        self.target("linux-py3.12", "idna\n")
        self.assertEqual(len(mirror.extras()[0]), 1)
        with mock.patch.dict(os.environ, {"MIRROR_BUNDLE_REQUIREMENTS": "off"}):
            self.assertEqual(mirror.extras(), ([], None))

    def test_api_url_and_project_override_the_ci_variables(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        url = f"http://127.0.0.1:{self.server.server_port}/other/v4"
        code, out = self.run_cli("--api-url", url, "--project", "grp/proj", "publish", "--dry-run")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.server.paths, ["/other/v4/projects/grp%2Fproj/packages/pypi/simple/idna"])
        self.assertEqual(os.environ["MIRROR_PROJECT"], "grp/proj")

    def test_each_mirror_variable_is_read(self):
        base = f"http://127.0.0.1:{self.server.server_port}/env/v4"
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        env = {"MIRROR_GITLAB_API_URL": base, "MIRROR_PROJECT": "7", "MIRROR_TOKEN": tm.TOKEN,
               "MIRROR_USERNAME": "someone"}
        with mock.patch.dict(os.environ, env, clear=True):
            code, out = self.run_cli("publish")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.server.paths[0], "/env/v4/projects/7/packages/pypi/simple/idna")
        self.assertIn("idna-3.20-py3-none-any.whl", tm.FakeGitLab.files["idna"])

    def test_the_old_unprefixed_names_are_not_read(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        env = {"GITLAB_API_URL": "http://127.0.0.1:1", "PYPI_PROJECT": "1", "PYPI_TOKEN": tm.TOKEN}
        with mock.patch.dict(os.environ, env, clear=True):
            code, _ = self.run_cli("publish")
        self.assertEqual(code, 1)
        self.assertIn("--api-url or MIRROR_GITLAB_API_URL (or CI_API_V4_URL)", self.err)
        self.assertIn("MIRROR_TOKEN (or CI_JOB_TOKEN)", self.err)

    def test_the_ca_bundle_variable_is_read_and_a_bad_path_is_an_error(self):
        with mock.patch.dict(os.environ, {"MIRROR_CA_BUNDLE": "/nonexistent/ca.pem"}):
            with self.assertRaises(OSError):
                mirror.Registry.from_env()

    def test_the_nifi_client_certificate_variables_are_read(self):
        url = "https://127.0.0.1:1/contentListener"
        path = Path(self.tmp.name) / "b.tar"
        path.write_bytes(b"x")
        env = {"MIRROR_NIFI_CLIENT_CERT": "/c/cert.pem", "MIRROR_NIFI_CLIENT_KEY": "/c/key.pem"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(mirror.ssl.SSLContext, "load_cert_chain") as load, \
                self.assertRaises(mirror.MirrorError):
            mirror.post_bundle(path, url, "delta")
        load.assert_called_once_with("/c/cert.pem", "/c/key.pem")

    def test_the_nifi_url_variable_is_the_default_of_nifi_url(self):
        tm.make_wheel(self.target("linux-py3.12", "idna\n"), "idna-3.20-py3-none-any.whl")
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        with mock.patch.dict(os.environ, {"MIRROR_NIFI_URL": url}):
            code, _ = self.run_cli("publish", "-t", str(Path(self.tmp.name) / "d"))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(len(self.server.posted), 1)

    def test_the_export_since_variable_is_the_default_of_since(self):
        os.environ["MIRROR_TOKEN"] = tm.TOKEN
        os.environ["MIRROR_USERNAME"] = "pat"
        self.server.now = "2026-09-20T10:00:00Z"
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        self.run_cli("publish")
        with mock.patch.dict(os.environ, {"MIRROR_EXPORT_SINCE": "2026-09-15"}):
            code, out = self.run_cli("export", "-t", str(Path(self.tmp.name) / "e"))
        self.assertEqual(code, 0, self.err)
        self.assertIn("received since 2026-09-15", self.err)

    # list and check

    def test_list_prints_the_table_on_stdout_and_nothing_on_stderr(self):
        self.target("linux-py3.12", "idna\n")
        self.target("windows-py3.9-arm64")
        code, out = self.run_cli("list")
        self.assertEqual((code, self.err), (0, ""))
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("linux-py3.12", out.splitlines()[0])
        self.assertIn("requirements=1", out.splitlines()[0])
        self.assertIn("win_arm64", out.splitlines()[1])

    def test_list_takes_target_operands_and_an_unknown_one_exits_1(self):
        self.target("linux-py3.12")
        self.target("windows-py3.9")
        code, out = self.run_cli("list", "windows-py3.9")
        self.assertEqual((code, len(out.splitlines())), (0, 1))
        code, out = self.run_cli("list", "linux-py3.99")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{mirror.prog()}: no such target: linux-py3.99", self.err)

    def test_check_is_silent_when_every_target_is_held_to_its_python(self):
        self.target("linux-py3.12")
        self.assertEqual(self.run_cli("check"), (0, ""))
        self.assertEqual(self.err, "")
        (self.packages / "py3.12-linux").mkdir()
        self.assertEqual(self.run_cli("check")[0], 1)
        self.assertIn("a target directory is named", self.err)

    def test_check_names_each_renovate_gap_on_stderr_with_exit_1(self):
        self.target("linux-py3.12")
        (self.packages.parent / "renovate.json").write_text('{"packageRules": []}')
        code, out = self.run_cli("check")
        self.assertEqual((code, out), (1, ""))
        lines = self.err.splitlines()
        self.assertTrue(all(line.startswith(f"{mirror.prog()}: ") for line in lines), lines)
        self.assertIn("constraintsFiltering", lines[0])
        self.assertIn("packages/linux-py3.12: renovate.json gives Python nothing", lines[1])

    # download

    def test_download_operands_reach_pip_and_pip_options_are_not_accepted(self):
        self.target("linux-py3.12", "idna\n")
        self.target("windows-py3.9", "idna\n")
        commands = []

        def run(command, check):
            commands.append(command)

        with mock.patch.object(mirror.subprocess, "run", side_effect=run):
            code, out = self.run_cli("download", "windows-py3.9")
            self.assertEqual((code, out), (0, ""))
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][commands[0].index("--python-version") + 1], "3.9")
            self.assertIn("==> windows-py3.9: download --dest", self.err)
            self.assertEqual(self.run_cli("download", "--pip-arg", "--no-deps")[0], 2)
            self.assertEqual(len(commands), 1)

    def test_a_failed_pip_download_exits_1_and_names_the_targets(self):
        self.target("linux-py3.12", "idna\n")

        def run(command, check):
            raise mirror.subprocess.CalledProcessError(1, command)

        with mock.patch.object(mirror.subprocess, "run", side_effect=run):
            code, out = self.run_cli("download")
        self.assertEqual((code, out), (1, ""))
        self.assertIn(f"{mirror.prog()}: 1 target(s) failed: linux-py3.12", self.err)

    # publish

    def test_publish_writes_the_tar_to_the_target_directory_and_prints_its_path(self):
        tm.make_wheel(self.target("linux-py3.12", "idna\n"), "idna-3.20-py3-none-any.whl")
        directory = Path(self.tmp.name) / "delta"
        for flag in ("-t", "--target-directory"):
            with self.subTest(flag):
                code, out = self.run_cli("publish", flag, str(directory))
                self.assertEqual(code, 0, self.err)
                if flag == "-t":
                    [tar] = directory.glob("pypi-delta-*.tar")
                    self.assertEqual(out, f"{tar}\n")
                    self.assertTrue(tar.with_name(tar.name + ".sha256").is_file())
                else:
                    self.assertIn("no bundle written", self.err)

    def test_nifi_url_without_a_target_directory_is_a_usage_error_but_the_variable_alone_is_ignored(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        self.assertEqual(self.run_cli("publish", "--nifi-url", url)[0], 2)
        self.assertEqual(tm.FakeGitLab.files, {})
        with mock.patch.dict(os.environ, {"MIRROR_NIFI_URL": url}):
            self.assertEqual(self.run_cli("publish")[0], 0)
        self.assertEqual(self.server.posted, [])

    def test_publish_posts_the_tar_to_nifi_url(self):
        tm.make_wheel(self.target("linux-py3.12", "idna\n"), "idna-3.20-py3-none-any.whl")
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        code, out = self.run_cli("publish", "-t", str(Path(self.tmp.name) / "d"), "--nifi-url", url)
        self.assertEqual(code, 0, self.err)
        self.assertEqual([name for name, _, _ in self.server.posted], [Path(out.strip()).name])
        self.assertIn("posted pypi-delta-", self.err)

    # export

    def export_setup(self):
        os.environ["MIRROR_TOKEN"], os.environ["MIRROR_USERNAME"] = tm.TOKEN, "pat"
        wheelhouse = self.target("linux-py3.12")
        self.target("windows-py3.12")
        for name in ("numpy-2.3.4-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl",
                     "numpy-2.3.4-cp312-cp312-win_amd64.whl", "idna-3.20-py3-none-any.whl",
                     "certifi-2026.7.22-py3-none-any.whl"):
            tm.make_wheel(wheelhouse, name)
        self.run_cli("publish")

    def wheels_in(self, tar):
        with tarfile.open(tar) as handle:
            return sorted(n[len("wheels/"):] for n in handle.getnames() if n.startswith("wheels/"))

    def test_export_takes_package_operands_and_a_target_filter(self):
        self.export_setup()
        out_dir = Path(self.tmp.name) / "pick"
        code, out = self.run_cli("export", "numpy==2.3.4", "idna", "--target", "windows-py3.12", "-t", str(out_dir))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.wheels_in(out.strip()), ["idna-3.20-py3-none-any.whl", "numpy-2.3.4-cp312-cp312-win_amd64.whl"])
        self.assertEqual(self.run_cli("export", "numpy==9.9", "-t", str(out_dir))[0], 1)
        self.assertIn("holds no wheel of numpy==9.9", self.err)

    def test_export_reads_packages_from_files_and_stdin(self):
        self.export_setup()
        listing = Path(self.tmp.name) / "list.txt"
        listing.write_text("# carry\nidna  # name only\n\n")
        out_dir = Path(self.tmp.name) / "pick"
        code, out = self.run_cli("export", "-T", str(listing), "--files-from", str(listing), "certifi", "-t", str(out_dir))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.wheels_in(out.strip()), ["certifi-2026.7.22-py3-none-any.whl", "idna-3.20-py3-none-any.whl"])
        with mock.patch.object(sys, "stdin", io.StringIO("certifi==2026.7.22\n")):
            code, out = self.run_cli("export", "-T", "-", "-t", str(out_dir))
        self.assertEqual(self.wheels_in(out.strip()), ["certifi-2026.7.22-py3-none-any.whl"])
        listing.write_text("bad spec!\n")
        self.assertEqual(self.run_cli("export", "-T", str(listing))[0], 2)
        self.assertEqual(self.run_cli("export", "-T", str(listing.with_name("missing.txt")))[0], 2)

    def test_export_by_date_excludes_packages_and_one_mode_is_required(self):
        self.export_setup()
        out_dir = Path(self.tmp.name) / "since"
        code, out = self.run_cli("export", "--since", "2026-08-01", "-t", str(out_dir))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(len(self.wheels_in(out.strip())), 4)
        self.assertEqual(self.run_cli("export", "--since", "2026-08-01", "idna")[0], 2)
        self.assertEqual(self.run_cli("export")[0], 2)

    def test_export_writes_to_the_working_directory_by_default(self):
        self.export_setup()
        work = Path(self.tmp.name) / "cwd"
        work.mkdir()
        previous = Path.cwd()
        os.chdir(work)
        try:
            code, out = self.run_cli("export", "idna")
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, self.err)
        [tar] = work.glob("pypi-pick-*.tar")
        self.assertEqual(Path(out.strip()).name, tar.name)
        self.assertTrue(tar.with_name(tar.name + ".sha256").is_file())
        self.assertEqual([p.name for p in work.iterdir() if p.name.startswith(".")], [])

    def test_export_output_names_the_tar_exactly_and_is_exclusive_with_a_directory(self):
        self.export_setup()
        tar = Path(self.tmp.name) / "carry" / "numpy.tar"
        for flag in ("-o", "--output"):
            code, out = self.run_cli("export", "numpy==2.3.4", flag, str(tar))
            self.assertEqual((code, out), (0, f"{tar}\n"), self.err)
        self.assertEqual(len(self.wheels_in(tar)), 2)
        self.assertEqual(tar.with_name("numpy.tar.sha256").read_text().split()[0], mirror.sha256_file(tar))
        self.assertEqual(self.run_cli("export", "idna", "-o", str(tar), "-t", str(tar.parent))[0], 2)

    def test_export_posts_the_tar_to_nifi_url(self):
        self.export_setup()
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        code, out = self.run_cli("export", "idna", "-t", str(Path(self.tmp.name) / "p"), "--nifi-url", url)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(self.server.post_headers[-1]["X-Bundle-Kind"], "pick")

    # import

    def two_bundles(self):
        """An inbox holding two bundles: certifi first, then idna with a changed requirements.txt."""
        tm.make_wheel(self.target("linux-py3.12", "certifi\n"), "certifi-2026.7.22-py3-none-any.whl")
        inbox = Path(self.tmp.name) / "inbox"
        self.run_cli("publish", "-t", str(inbox))
        (self.packages / "linux-py3.12" / "requirements.txt").write_text("certifi\nidna\n")
        tm.make_wheel(self.packages.parent / "wheelhouse", "idna-3.20-py3-none-any.whl")
        self.run_cli("publish", "-t", str(inbox))
        tm.FakeGitLab.files = {}
        return inbox

    def test_a_directory_is_an_inbox_imported_oldest_first_and_removed(self):
        inbox = self.two_bundles()
        high = Path(self.tmp.name) / "high"
        self.assertEqual(len(list(inbox.glob("pypi-*"))), 4)
        code, out = self.run_cli("import", str(inbox), "--requirements", str(high))
        self.assertEqual((code, out), (0, ""), self.err)
        self.assertIn("2 file(s): 2 uploaded", self.err)
        self.assertEqual((high / "linux-py3.12" / "requirements.txt").read_text(), "certifi\nidna\n")  # newest won
        self.assertEqual(list(inbox.iterdir()), [])
        code, out = self.run_cli("import", str(inbox))  # an empty inbox is not an error
        self.assertEqual(code, 0, self.err)
        self.assertIn("0 file(s)", self.err)

    def test_keep_leaves_the_inbox_files_where_they_are(self):
        inbox = self.two_bundles()
        for flag in ("-k", "--keep"):
            code, _ = self.run_cli("import", str(inbox), flag)
            self.assertEqual(code, 0, self.err)
            self.assertEqual(len(list(inbox.glob("pypi-*.tar"))), 2)
            self.assertEqual(len(list(inbox.glob("pypi-*.sha256"))), 2)
            tm.FakeGitLab.files = {}

    def test_import_dry_run_prints_the_plan_and_changes_nothing(self):
        inbox = self.two_bundles()
        before = sorted(p.name for p in inbox.iterdir())
        for flag in ("-n", "--dry-run"):
            code, out = self.run_cli("import", str(inbox), flag)
            self.assertEqual(code, 0, self.err)
            lines = out.splitlines()
            self.assertEqual(sum(line.startswith("would upload ") for line in lines), 2)
            self.assertEqual(sorted(line.split(": ", 1)[1].rsplit("/", 1)[1] for line in lines if line.startswith("would remove: ")),
                             before)
            self.assertEqual(tm.FakeGitLab.files, {})
            self.assertEqual(sorted(p.name for p in inbox.iterdir()), before)

    def test_a_tar_that_fails_stays_in_the_inbox_and_a_missing_source_exits_1(self):
        inbox = self.two_bundles()
        first, second = sorted(inbox.glob("pypi-*.tar"))
        with open(second, "ab") as handle:
            handle.write(b"x")  # now it fails its checksum
        code, _ = self.run_cli("import", str(inbox))
        self.assertEqual(code, 1)
        self.assertIn("checksum does not match", self.err)
        self.assertEqual(sorted(inbox.glob("pypi-*.tar")), [second], "the first was imported and removed")
        self.assertFalse(first.exists())
        self.assertEqual(self.run_cli("import", str(Path(self.tmp.name) / "missing"))[0], 1)
        self.assertIn("no such file or directory", self.err)

    def test_several_tars_import_in_the_order_given_and_are_kept(self):
        inbox = self.two_bundles()
        first, second = sorted(inbox.glob("pypi-*.tar"))
        high = Path(self.tmp.name) / "high"
        code, _ = self.run_cli("import", str(first), str(second), "--requirements", str(high))
        self.assertEqual(code, 0, self.err)
        self.assertTrue(first.exists() and second.exists())
        self.assertEqual((high / "linux-py3.12" / "requirements.txt").read_text(), "certifi\nidna\n")

    def test_import_git_bundle_writes_the_history_file(self):
        inbox = self.two_bundles()
        target = Path(self.tmp.name) / "repo.bundle"
        code, _ = self.run_cli("import", str(inbox), "--git-bundle", str(target), "-k")
        self.assertEqual(code, 0, self.err)
        self.assertFalse(target.exists(), "bundles made without BUNDLE_GIT hold no history")

    # -n / --dry-run

    def tree(self):
        root = self.packages.parent
        return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else b"") for p in root.rglob("*"))

    def test_pin_dry_run_prints_the_pins_and_leaves_requirements_alone(self):
        self.target("linux-py3.9", "requests\nPyYAML  # yaml\n")
        _, pip = self.fake_pip("requests-2.31.0-py3-none-any.whl", "PyYAML-6.0.2-cp39-cp39-manylinux_2_17_x86_64.whl")
        before = self.tree()
        for flag in ("-n", "--dry-run"):
            with pip:
                code, out = self.run_cli("pin", flag)
            self.assertEqual(code, 0, self.err)
            self.assertEqual(out.splitlines(), ["would pin linux-py3.9: requests==2.31.0",
                                                "would pin linux-py3.9: PyYAML==6.0.2"])
            self.assertEqual(self.tree(), before)
        with pip:
            code, out = self.run_cli("pin")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("linux-py3.9: requests==2.31.0", self.err)

    def fake_pip(self, *wheels):
        def run(command, check):
            dest = Path(command[command.index("--dest") + 1])
            for wheel in wheels:
                (dest / wheel).write_bytes(b"")

        return [], mock.patch.object(mirror.subprocess, "run", side_effect=run)

    def test_prune_dry_run_prints_would_remove_and_deletes_nothing(self):
        wheelhouse = self.target("linux-py3.12")
        published = tm.make_wheel(wheelhouse, "idna-3.20-py3-none-any.whl")
        tm.make_wheel(wheelhouse, "certifi-2026.7.22-py3-none-any.whl")
        tm.FakeGitLab.files["idna"] = {published.name: {}}
        before = self.tree()
        for flag in ("-n", "--dry-run"):
            code, out = self.run_cli("prune", flag)
            self.assertEqual((code, out), (0, f"would remove: {published}\n"), self.err)
            self.assertEqual(self.tree(), before)
        self.assertEqual(self.run_cli("prune"), (0, ""))
        self.assertFalse(published.exists())

    def test_publish_dry_run_changes_nothing_even_in_an_older_wheelhouse(self):
        self.target("linux-py3.12")
        old = self.packages / "linux-py3.12" / "wheelhouse"
        old.mkdir()
        tm.make_wheel(old, "idna-3.20-py3-none-any.whl")
        before = self.tree()
        code, out = self.run_cli("publish", "-n")
        self.assertEqual((code, out), (0, "would upload idna-3.20-py3-none-any.whl\n"), self.err)
        self.assertEqual(self.tree(), before)
        self.assertEqual(tm.FakeGitLab.files, {})

    def test_publish_dry_run_prints_the_bundle_and_post_plan(self):
        tm.make_wheel(self.target("linux-py3.12", "idna\n"), "idna-3.20-py3-none-any.whl")
        directory = Path(self.tmp.name) / "delta"
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        before = self.tree()
        code, out = self.run_cli("publish", "--dry-run", "-t", str(directory), "--nifi-url", url)
        self.assertEqual(code, 0, self.err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "would upload idna-3.20-py3-none-any.whl")
        self.assertRegex(lines[1], rf"^would bundle 1 file\(s\) into {directory}/pypi-delta-.*\.tar$")
        self.assertEqual(lines[2], f"would post to {url}")
        self.assertFalse(directory.exists())
        self.assertEqual(self.server.posted, [])
        self.assertEqual(self.tree(), before)

    def test_export_dry_run_prints_the_files_and_the_tar_and_downloads_nothing(self):
        self.export_setup()
        directory = Path(self.tmp.name) / "pick"
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        for flag in ("-n", "--dry-run"):
            code, out = self.run_cli("export", "idna", flag, "-t", str(directory), "--nifi-url", url)
            self.assertEqual(code, 0, self.err)
            lines = out.splitlines()
            self.assertEqual(lines[0], "would add idna-3.20-py3-none-any.whl")
            self.assertRegex(lines[1], rf"^would write {directory}/pypi-pick-.*\.tar \(1 wheel\(s\), 2 requirements file\(s\)\)$")
            self.assertEqual(lines[2], f"would post to {url}")
            self.assertFalse(directory.exists())
            self.assertEqual(self.server.posted, [])
        code, out = self.run_cli("export", "idna", "-n", "-o", str(directory / "x.tar"))
        self.assertIn(f"would write {directory / 'x.tar'} ", out)
        self.assertFalse(directory.exists())

    # export -o -

    def test_export_to_stdout_streams_a_tar_that_import_accepts(self):
        self.export_setup()
        before = sorted(p.name for p in Path(tempfile.gettempdir()).glob("pypi-mirror-*"))
        code, out = self.run_cli("export", "numpy==2.3.4", "--target", "windows-py3.12", "-o", "-")
        self.assertEqual(code, 0, self.err)
        with tarfile.open(fileobj=io.BytesIO(self.out_bytes)) as tar:
            self.assertEqual(sorted(n for n in tar.getnames() if n.startswith("wheels/")),
                             ["wheels/numpy-2.3.4-cp312-cp312-win_amd64.whl"])
        self.assertIn("bundle: ", self.err, "progress stays on stderr")
        self.assertRegex(self.err, r"bundle: \S*/pypi-pick-\d{8}T\d{9}Z\.tar ")
        self.assertEqual(sorted(p.name for p in Path(tempfile.gettempdir()).glob("pypi-mirror-*")), before,
                         "the working tar is removed")
        tar_file = Path(self.tmp.name) / "carried.tar"
        tar_file.write_bytes(self.out_bytes)
        self.assertFalse(tar_file.with_name("carried.tar.sha256").exists())
        tm.FakeGitLab.files = {}
        code, _ = self.run_cli("import", str(tar_file))
        self.assertEqual(code, 0, self.err)
        self.assertIn("numpy-2.3.4-cp312-cp312-win_amd64.whl", tm.FakeGitLab.files["numpy"])

    def test_export_to_stdout_conflicts_with_a_directory_and_with_nifi_url(self):
        self.export_setup()
        url = f"http://127.0.0.1:{self.server.server_port}/contentListener"
        self.assertEqual(self.run_cli("export", "idna", "-o", "-", "--nifi-url", url)[0], 2)
        self.assertEqual(self.run_cli("export", "idna", "-o", "-", "-t", self.tmp.name)[0], 2)
        with mock.patch.dict(os.environ, {"MIRROR_NIFI_URL": url}):
            self.assertEqual(self.run_cli("export", "idna", "-o", "-")[0], 0, self.err)
        self.assertEqual(self.server.posted, [])

    def test_export_to_stdout_writes_nothing_else_there_and_errors_leave_it_empty(self):
        self.export_setup()
        code, out = self.run_cli("export", "numpy==9.9", "-o", "-")
        self.assertEqual((code, self.out_bytes), (1, b""))
        self.assertIn("holds no wheel of numpy==9.9", self.err)
        self.assertEqual(list(Path.cwd().glob("pypi-*.tar")), [])
        code, out = self.run_cli("export", "idna", "-o", "-", "-n")
        self.assertEqual(out.splitlines()[-1].split(" (")[0], "would write standard output")


BIG_MIB = 40  # above the 1 MiB chunk by a wide margin; the checks below fail on any whole-file read


def make_big_wheel(directory: Path, filename: str, mib: int = BIG_MIB) -> Path:
    """A valid wheel whose payload is mib MiB of zeros, written a chunk at a time."""
    name, version = mirror.name_and_version(filename)
    path = directory / filename
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        with archive.open(f"{name}/payload.bin", "w", force_zip64=True) as payload:
            for _ in range(mib):
                payload.write(bytes(1 << 20))
    return path


class ReadGuard:
    """Makes any read of a whole file fail: Path.read_bytes, and read() with no size or one over a chunk, in mirror.py."""

    def __init__(self, testcase):
        self.testcase = testcase

    def __enter__(self):
        real_open = open
        testcase = self.testcase

        class Proxy:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                testcase.assertTrue(0 < size <= mirror.CHUNK, f"read({size}) would hold more than one chunk in memory")
                return self._handle.read(size)

            def __getattr__(self, name):
                return getattr(self._handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        def guarded(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            return Proxy(handle) if "b" in mode and "r" in mode else handle

        def refuse(*args, **kwargs):
            raise AssertionError("Path.read_bytes() reads the whole file into memory")

        self.patches = [mock.patch.object(mirror, "open", guarded, create=True),
                        mock.patch.object(Path, "read_bytes", refuse)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


class LargeWheelTests(tm.CliMixin, tm.FakeRegistry):
    """The one-package manual path moves wheels of any size: nothing reads a whole wheel into memory."""

    def setUp(self):
        super().setUp()
        os.environ["MIRROR_TOKEN"], os.environ["MIRROR_USERNAME"] = tm.TOKEN, "pat"
        self.wheel = make_big_wheel(self.target("linux-py3.12", "bigpkg\n"), "bigpkg-1.0-py3-none-any.whl")
        self.sha = mirror.sha256_file(self.wheel)

    def test_the_upload_body_is_streamed_from_disk_in_chunks(self):
        sent = {}

        def capture(registry, request, timeout=120):
            body = request.data
            sent["is_bytes"] = isinstance(body, (bytes, bytearray))
            sent["length"] = int(request.get_header("Content-length"))
            total, biggest = 0, 0
            while chunk := body.read(8192):
                total += len(chunk)
                biggest = max(biggest, len(chunk))
            sent.update(total=total, biggest=biggest, timeout=timeout)
            return mock.MagicMock()

        with mock.patch.object(mirror.Registry, "_request", capture):
            mirror.Registry.from_env().upload(self.wheel)
        self.assertFalse(sent["is_bytes"])
        self.assertEqual(sent["total"], sent["length"])
        self.assertGreater(sent["total"], BIG_MIB << 20)
        self.assertLessEqual(sent["biggest"], 8192)
        self.assertGreaterEqual(sent["timeout"], 600, "GitLab answers a multi-GB upload only after storing it")

    def test_publish_export_and_import_of_a_large_wheel_never_read_it_whole(self):
        out = Path(self.tmp.name) / "carry"
        with ReadGuard(self):
            self.assertEqual(self.run_cli("publish")[0], 0, self.err)
            fields = tm.FakeGitLab.files["bigpkg"][self.wheel.name]
            self.assertEqual(fields["sha256_digest"], self.sha)
            code, tar = self.run_cli("export", "bigpkg==1.0", "-o", str(out / "bigpkg.tar"))
            self.assertEqual(code, 0, self.err)
            tm.FakeGitLab.files = {}
            code, _ = self.run_cli("import", str(out / "bigpkg.tar"))
            self.assertEqual(code, 0, self.err)
        self.assertEqual(tm.FakeGitLab.files["bigpkg"][self.wheel.name]["sha256_digest"], self.sha)

    def test_export_to_stdout_of_a_large_wheel_streams_it(self):
        with ReadGuard(self):
            self.run_cli("publish")
            code, _ = self.run_cli("export", "bigpkg", "-o", "-")
        self.assertEqual(code, 0, self.err)
        with tarfile.open(fileobj=io.BytesIO(self.out_bytes)) as tar:
            member = tar.getmember(f"wheels/{self.wheel.name}")
            self.assertEqual(member.size, self.wheel.stat().st_size)

    def test_a_size_refusal_is_named_with_the_limit_to_raise(self):
        for status, body in ((413, "Request Entity Too Large"), (400, '{"message":"File is too large"}')):
            with self.subTest(status):
                tm.FakeGitLab.files = {}
                self.server.upload_refusal = (status, body)
                code, out = self.run_cli("publish")
                self.assertEqual((code, out), (1, ""))
                self.assertIn(f"{mirror.prog()}: upload {self.wheel.name}: HTTP {status}", self.err)
                self.assertIn("max_pypi_package_file_size", self.err)
                self.assertIn("5 GB", self.err)
                self.assertEqual(tm.FakeGitLab.files, {})

    def test_a_connection_closed_during_the_upload_names_the_limit_too(self):
        real = mirror.Registry._request

        def reset_on_upload(registry, request, timeout=120):
            if request.get_method() == "POST":
                raise ConnectionResetError(54, "Connection reset by peer")
            return real(registry, request, timeout)

        with mock.patch.object(mirror.Registry, "_request", reset_on_upload):
            code, _ = self.run_cli("publish")
        self.assertEqual(code, 1)
        self.assertIn("Connection reset by peer", self.err)
        self.assertIn("client_max_body_size", self.err)

    def test_another_refusal_does_not_blame_the_size(self):
        self.server.upload_refusal = (403, "Forbidden")
        code, _ = self.run_cli("publish")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 403: Forbidden", self.err)
        self.assertNotIn("max_pypi_package_file_size", self.err)


@unittest.skipUnless(
    all(__import__("shutil").which(t) for t in ("curl", "unzip", "sha256sum")), "publish.sh needs curl, unzip, sha256sum"
)
class PublishShellArgumentTests(tm.FakeRegistry):
    def run_sh(self, *argv):
        env = dict(os.environ, MIRROR_WHEELHOUSE_DIR=str(self.packages.parent / "wheelhouse"), PATH=tm.SHELL_PATH)
        result = subprocess.run(["sh", str(Path(mirror.__file__).with_name("publish.sh")), *argv],
                                env=env, capture_output=True, text=True)
        return result.returncode, result.stdout, result.stderr

    def test_an_unknown_argument_exits_2_and_uploads_nothing(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        for argument in ("--bogus", "-x", "extra"):
            with self.subTest(argument):
                code, out, err = self.run_sh(argument)
                self.assertEqual((code, out), (2, ""))
                self.assertIn(f"publish.sh: unknown argument: {argument}", err)
                self.assertIn("usage: publish.sh", err)
                self.assertEqual(tm.FakeGitLab.files, {})

    def test_dry_run_prints_the_plan_on_stdout_and_progress_on_stderr(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        for flag in ("-n", "--dry-run"):
            with self.subTest(flag):
                code, out, err = self.run_sh(flag)
                self.assertEqual((code, out), (0, "would upload idna-3.20-py3-none-any.whl\n"))
                self.assertIn("1 file(s): 1 to upload", err)
                self.assertEqual(tm.FakeGitLab.files, {})

    def test_quiet_drops_progress_only(self):
        tm.make_wheel(self.target("linux-py3.12"), "idna-3.20-py3-none-any.whl")
        for flag in ("-q", "--quiet"):
            with self.subTest(flag):
                code, out, err = self.run_sh(flag, "-n")
                self.assertEqual((code, out, err), (0, "would upload idna-3.20-py3-none-any.whl\n", ""))
        os.environ["CI_JOB_TOKEN"] = "wrong"
        code, out, err = self.run_sh("-q")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("publish.sh: GET", err)

    def test_help_exits_0_with_the_usage_on_stdout(self):
        for flag in ("-h", "--help"):
            with self.subTest(flag):
                code, out, err = self.run_sh(flag)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("usage: publish.sh", out)

    def test_errors_and_warnings_carry_the_script_name(self):
        wheelhouse = self.target("linux-py3.12")
        (wheelhouse / "pkg-0.1.tar.gz").write_bytes(b"x")
        code, out, err = self.run_sh("-n")
        self.assertIn("publish.sh: warning: ignoring wheelhouse/pkg-0.1.tar.gz", err)
        os.environ.pop("CI_JOB_TOKEN")
        code, out, err = self.run_sh()
        self.assertEqual(code, 1)
        self.assertIn("publish.sh: set MIRROR_TOKEN", err)


@unittest.skipUnless(__import__("shutil").which("bash"), "sync.sh needs bash")
class SyncShellTests(unittest.TestCase):
    """sync.sh with a stub git and a stub interpreter, so no network and no repository are touched."""

    def setUp(self):
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "bin").mkdir()
        (root / "repo").mkdir()
        (root / "repo" / "sync.sh").write_text((Path(mirror.__file__).with_name("sync.sh")).read_text())
        for name in ("git", "python-stub"):
            stub = root / "bin" / name
            # git diff --cached --quiet exits 1: there is something to commit
            stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{root}/calls"\n[ "$1" = diff ] && exit 1\nexit 0\n')
            stub.chmod(0o755)
        self.calls = root / "calls"

    def run_sync(self, *argv):
        env = {"PATH": f"{self.tmp.name}/bin:/usr/bin:/bin", "PYTHON": "python-stub", "HOME": self.tmp.name}
        result = subprocess.run(["bash", str(Path(self.tmp.name) / "repo" / "sync.sh"), *argv],
                                env=env, capture_output=True, text=True)
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return result.returncode, result.stdout, result.stderr, calls

    def test_help_prints_the_usage_and_exits_0(self):
        for flag in ("-h", "--help"):
            with self.subTest(flag):
                code, out, err, calls = self.run_sync(flag)
                self.assertEqual((code, err, calls), (0, "", []))
                self.assertIn("usage: sync.sh", out)
                self.assertIn("--no-push", out)

    def test_an_unknown_argument_exits_2_before_anything_runs(self):
        for argument in ("--bogus", "-x", "--target"):
            with self.subTest(argument):
                code, out, err, calls = self.run_sync(argument)
                self.assertEqual((code, out, calls), (2, "", []))
                self.assertIn(f"sync.sh: unknown argument: {argument}", err)
                self.assertIn("usage: sync.sh", err)
                self.assertNotIn("unbound", err)

    def test_target_operands_go_to_download_in_order(self):
        code, out, err, calls = self.run_sync("linux-py3.12", "windows-py3.9")
        self.assertEqual(code, 0, err)
        self.assertIn("python-stub mirror.py download linux-py3.12 windows-py3.9", calls)
        self.assertNotIn("python-stub mirror.py prune", calls)

    def test_no_operands_download_every_target(self):
        code, out, err, calls = self.run_sync()
        self.assertIn("python-stub mirror.py download", calls)

    def test_prune_runs_after_the_download(self):
        code, out, err, calls = self.run_sync("--prune", "--no-push")
        self.assertEqual(code, 0, err)
        python_calls = [c for c in calls if c.startswith("python-stub")]
        self.assertEqual(python_calls, ["python-stub mirror.py download", "python-stub mirror.py prune"])

    def test_no_push_never_pushes(self):
        code, out, err, calls = self.run_sync("--no-push")
        self.assertNotIn("git push", calls)
        self.assertTrue(any(c.startswith("git commit") for c in calls))

    def test_the_default_commits_and_pushes(self):
        code, out, err, calls = self.run_sync()
        self.assertEqual(code, 0, err)
        self.assertIn("git push", calls)


class ParserTreeTests(unittest.TestCase):
    """A new option or command fails here until it has help text and a test that names it."""

    def actions(self):
        parser = mirror.build_parser()
        found = [(None, action) for action in parser._actions]
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    found += [(name, inner) for inner in sub._actions]
        return found, [name for action in parser._actions if isinstance(action, argparse._SubParsersAction)
                       for name in action.choices]

    def test_every_option_and_operand_has_help(self):
        found, commands = self.actions()
        self.assertEqual(sorted(commands), ["check", "download", "export", "import", "list", "pin", "prune", "publish"])
        for command, action in found:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)) or action.help == argparse.SUPPRESS:
                continue
            with self.subTest(command=command, option=action.option_strings or action.dest):
                self.assertTrue(isinstance(action.help, str) and action.help.strip(), "add a help string")

    def test_every_option_and_command_is_named_in_a_test(self):
        source = Path(__file__).read_text()
        found, commands = self.actions()
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(f'"{command}"', source, f"no test runs the {command} command")
        for command, action in found:
            if isinstance(action, argparse._HelpAction):
                continue
            for option in action.option_strings:
                with self.subTest(command=command, option=option):
                    self.assertIn(f'"{option}"', source, f"no test passes {option}")

    def test_every_command_has_examples_in_its_help(self):
        for name, sub in next(a for a in mirror.build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices.items():
            with self.subTest(name):
                self.assertIn("examples:", sub.epilog)
                self.assertTrue(sub.description)
                self.assertIn("which ", sub.epilog, "say which packages or files the command acts on")
                self.assertIn("check it:", sub.epilog, "say how to check the result")
                if name in ("publish", "export", "import", "download"):
                    self.assertTrue(any(word in sub.epilog for word in ("destination:", "source")), "name sources or destinations")

    def test_the_readme_lists_every_command_option_and_variable(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        table = readme.split("## Commands", 1)[1].split("### Exit codes", 1)[0]
        found, commands = self.actions()
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(f"| `{command}` |", table)
        for command, action in found:
            if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
                continue
            for option in action.option_strings:
                with self.subTest(command=command, option=option):
                    self.assertIn(f"`{option}", table + readme.split("## Commands", 1)[1])
        source = (Path(__file__).resolve().parents[1] / "mirror.py").read_text()
        environment = readme.split("## Environment", 1)[1]
        for name in sorted(set(__import__("re").findall(r'"(MIRROR_[A-Z_]+)"', source))):
            with self.subTest(variable=name):
                self.assertIn(f"`{name}`", environment)


if __name__ == "__main__":
    unittest.main()
