"""Async subprocess helpers built on Gio.Subprocess (no threads, GTK main loop safe)."""
from gi.repository import Gio, GLib


class Runner:
    """Run a command, stream merged stdout/stderr line by line."""

    def __init__(self, on_line, on_done):
        self.on_line = on_line
        self.on_done = on_done
        self.proc = None
        self.stream = None

    def run(self, argv):
        flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE
        try:
            self.proc = Gio.Subprocess.new(argv, flags)
        except GLib.Error as e:
            self.on_line(f"error: {e.message}")
            self.on_done(-1)
            return
        self.stream = Gio.DataInputStream.new(self.proc.get_stdout_pipe())
        self.stream.set_newline_type(Gio.DataStreamNewlineType.ANY)  # \n, \r, \r\n
        self._read()

    def _read(self):
        self.stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self._got_line)

    def _got_line(self, stream, res):
        try:
            line, _ = stream.read_line_finish_utf8(res)
        except GLib.Error as e:
            self.on_line(f"error: {e.message}")
            line = None
        if line is None:  # EOF
            self.proc.wait_async(None, self._exited)
            return
        if line:
            self.on_line(line)
        self._read()

    def _exited(self, proc, res):
        try:
            proc.wait_finish(res)
        except GLib.Error:
            pass
        self.on_done(proc.get_exit_status() if proc.get_if_exited() else -1)


def capture(argv, callback, env=None, timeout=None):
    """Run a command, call callback(status, stdout, stderr) when finished.
    env: optional dict of extra environment variables (e.g. {"LC_ALL": "C"}).
    timeout: seconds; the process is killed and callback(-1, ...) fires if it runs longer
    (keeps network-dependent commands from hanging the UI on a weak connection)."""
    flags = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE
    try:
        if env:
            launcher = Gio.SubprocessLauncher.new(flags)
            for k, v in env.items():
                launcher.setenv(k, v, True)
            proc = launcher.spawnv(argv)
        else:
            proc = Gio.Subprocess.new(argv, flags)
    except GLib.Error as e:
        callback(-1, "", e.message)
        return

    timer = [0]
    timed_out = [False]

    def expire():
        timer[0] = 0
        timed_out[0] = True
        proc.force_exit()
        return False

    def done(p, res):
        if timer[0]:
            GLib.source_remove(timer[0])
            timer[0] = 0
        if timed_out[0]:
            callback(-1, "", "timed out")
            return
        try:
            _ok, out, err = p.communicate_utf8_finish(res)
        except GLib.Error as e:
            callback(-1, "", e.message)
            return
        status = p.get_exit_status() if p.get_if_exited() else -1
        callback(status, out or "", err or "")

    if timeout:
        timer[0] = GLib.timeout_add_seconds(int(timeout), expire)
    proc.communicate_utf8_async(None, None, done)
