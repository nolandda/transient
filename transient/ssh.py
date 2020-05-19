import logging
import signal
import subprocess
import time
import tempfile

from typing import Optional, List, IO, Any, Union

try:
    import importlib.resources as pkg_resources
    _package_read_text = pkg_resources.read_text  # type: ignore
except ImportError:
    # Try backported to PY<37 `importlib_resources`.
    import importlib_resources as pkg_resources  # type: ignore
    _package_read_text = pkg_resources.read_text  # type: ignore

from . import linux
from . import vagrant_keys

SSH_CONNECTION_WAIT_TIME = 3
SSH_CONNECTION_TIME_BETWEEN_TRIES = 2
SSHFS_MAX_RUN_TIME = 2

# From the typeshed Popen definitions
_FILE = Union[None, int, IO[Any]]


class SshClient:
    host: str
    port: int
    ssh_bin_name: str
    args: List[str]
    user: Optional[str]
    password: Optional[str]
    command: Optional[str]
    input: Optional[str]

    def __init__(self, *, host: str, port: Optional[int], ssh_bin_name: Optional[str],
                 user: Optional[str] = None, password: Optional[str] = None,
                 args: Optional[List[str]] = None, command: Optional[str] = None):
        self.host = host
        self.port = port if port is not None else 22
        self.user = user
        self.password = password
        self.args = args or []
        self.ssh_bin_name = ssh_bin_name or self.__find_ssh_bin_name()
        self.command = command

        # Pass these as default args
        self.args.extend(self.__default_ssh_args())

    def __default_ssh_args(self) -> List[str]:
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "batchMode=yes", "-o", "ConnectTimeout={}".format(SSH_CONNECTION_WAIT_TIME-1)]

    def __find_ssh_bin_name(self) -> str:
        return "ssh"

    def __prepare_builtin_keys(self) -> List[str]:
        vagrant_priv = _package_read_text(vagrant_keys, 'vagrant')
        _, vagrant_priv_file = tempfile.mkstemp()
        with open(vagrant_priv_file, "w") as f:
            f.write(vagrant_priv)
        return [vagrant_priv_file]

    def __prepare_ssh_command(self, user_cmd: Optional[str]) -> List[str]:
        if self.user is not None:
            host = "{}@{}".format(self.user, self.host)
        else:
            host = self.host

        args = self.args + self.__default_ssh_args() + ["-p", str(self.port)]

        priv_keys = self.__prepare_builtin_keys()
        for key in priv_keys:
            args.extend(["-i", key])

        command = [self.ssh_bin_name] + args + [host]
        if user_cmd is not None:
            command.append(user_cmd)

        return command

    def __timed_connection(self, timeout: int,
                           ssh_stdin: Optional[_FILE] = None,
                           ssh_stdout: Optional[_FILE] = None,
                           ssh_stderr: Optional[_FILE] = None) -> 'subprocess.Popen[bytes]':
        probe_command = self.__prepare_ssh_command("exit")
        real_command = self.__prepare_ssh_command(self.command)

        logging.info("Probing SSH using command '{}'".format(" ".join(probe_command)))

        start = time.time()
        while time.time() - start < timeout:
            # This process is just used to determine if SSH is available. It is
            # not connected to the requested pipes.
            proc = subprocess.Popen(
                probe_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,

                # Automatically send SIGTERM to this process when the main Transient
                # process dies
                preexec_fn=lambda: linux.set_death_signal(signal.SIGTERM))

            # We set the ConnectTimeout timeout option to less than SSH_CONNECTION_WAIT_TIME,
            # therefore, we don't need to catch any timeouts from this. If a connection
            # takes longer than the wait time, SSH will kill it. Once the connection is
            # established, we are only running 'exit'.
            returncode = proc.wait(SSH_CONNECTION_WAIT_TIME)

            # From the man pages: "ssh exits with the exit status of the
            # remote command or with 255 if an error occurred."
            if returncode == 255:
                _, raw_stderr = proc.communicate()
                stderr = raw_stderr.decode("utf-8").strip()
                logging.info("SSH connection failed: {}".format(stderr))
                # In many cases, the command will fail quickly. Avoid spamming tries
                time.sleep(SSH_CONNECTION_TIME_BETWEEN_TRIES)
                continue
            elif returncode == 0:
                # The command exited quickly which _should_ indicate that ssh is now
                # available in the guest (as the 'command' here is just 'exit'). Now
                # kill this connection and establish another that's connected to the
                # requested stdout/stderr
                proc.terminate()

                logging.info("Connecting to SSH using command '{}'".format(
                    " ".join(real_command)))

                proc = subprocess.Popen(
                    real_command,
                    stdin=ssh_stdin,
                    stdout=ssh_stdout,
                    stderr=ssh_stderr,
                    preexec_fn=lambda: linux.set_death_signal(signal.SIGTERM))
                return proc
            else:
                # If the process exited within SSH_CONNECTION_WAIT_TIME seconds with
                # any other return code, that's an exception.
                raise RuntimeError("ssh connection failed with return code: {}".format(
                    returncode))
        raise RuntimeError("Failed to connect with command '{}' after {} seconds".format(
            probe_command, timeout))

    def connect_wait(self, timeout: int) -> int:
        conn = self.__timed_connection(timeout)
        conn.wait()
        return conn.returncode

    def connect_piped(self, timeout: int) -> 'subprocess.Popen[bytes]':
        return self.__timed_connection(timeout,
                                       ssh_stdin=subprocess.PIPE,
                                       ssh_stdout=subprocess.PIPE,
                                       ssh_stderr=subprocess.PIPE)


def do_sshfs_mount(*, timeout: int, local_dir: str, remote_dir: str, host: str,
                   local_user: str, port: Optional[int] = None,
                   ssh_bin_name: Optional[str] = None,
                   remote_user: Optional[str] = None,
                   remote_password: Optional[str] = None,
                   local_password: Optional[str] = None,
                   ssh_args: Optional[List[str]] = None) -> None:

    client = SshClient(host=host, port=port, ssh_bin_name=ssh_bin_name,
                       user=remote_user, password=remote_password,
                       args=["-A", "-T", "-o", "LogLevel=ERROR"])
    conn = client.connect_piped(timeout=timeout)

    try:
        sshfs_command = "sudo -E sshfs -o allow_other {}@10.0.2.2:{} {}".format(
            local_user, local_dir, remote_dir)

        logging.info("Sending sshfs mount command '{}'".format(sshfs_command))

        # This is somewhat gnarly. The core of the issue is that sshfs is a FUSE mount,
        # so it runs as a process (that gets backgrounded by default). SSH won't close
        # the connection on it's side until "it encounters end-of-file (eof) on the pipes
        # connecting  to the stdout and stderr of the user program". This typically means
        # you can do something like 'nohup <cmd> >/dev/null </dev/null 2>&1 &' to close
        # all handles and ignore any hang ups. However, this doesn't work for SSHFS, as
        # it spawns other processes that (I guess?) still have an open handle.
        #
        # This causes the SSH connetion to hang forever after the logout. Therefore, we
        # need to close the connection on our end. So the trick here is to wait for some
        # max time for this process to be done, then inspect the stdout for a sentinel
        # value indicating that we _did_ get to the point where it should be OK to
        # terminate the connection.
        #
        # See http://www.snailbook.com/faq/background-jobs.auto.html for some more info.
        _, raw_stderr = conn.communicate(input="""
          set -e
          {sshfs_command}
          echo TRANSIENT_SSHFS_DONE
          exit
        """.format(sshfs_command=sshfs_command).encode('utf-8'), timeout=SSHFS_MAX_RUN_TIME)

        # On some platforms, maybe this does actually terminate. If it does,
        # then just return
        if conn.returncode != 0:
            raise RuntimeError("SSHFS mount failed with: {}".format(raw_stderr.decode('utf-8')))
        else:
            return
    except subprocess.TimeoutExpired:
        # The timeout expired (as expected), but because we 'set -e', this means
        # we must be in the state where we're hanging after the logout. So kill
        # the connection from our end, the sshfs process will continue on the
        # guest side.
        conn.terminate()

        # There is a chance the sshfs process hung, so check for the sentinel text
        raw_stdout, raw_stderr = conn.communicate()
        stdout = raw_stdout.decode('utf-8')
        stderr = raw_stderr.decode('utf-8')
        if "TRANSIENT_SSHFS_DONE" not in stdout:
            raise RuntimeError("SSHFS mount timed out: {}".format(stderr))
