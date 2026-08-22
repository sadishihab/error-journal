"""Stress test: realistic, messy error output as actually pasted by users."""

from fingerprint import fingerprint

SAMPLES = [
    # ---------------------------------------------------------- python ---
    ("py: chained exception (During handling...)", """Traceback (most recent call last):
  File "/app/db.py", line 44, in connect
    conn = psycopg2.connect(dsn)
psycopg2.OperationalError: could not connect to server: Connection refused

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/main.py", line 12, in <module>
    start()
  File "/app/main.py", line 8, in start
    db.connect()
RuntimeError: database unavailable"""),

    ("py: nested with __cause__", """Traceback (most recent call last):
  File "app.py", line 3, in <module>
    from mylib import thing
ImportError: cannot import name 'thing' from 'mylib' (/usr/lib/mylib/__init__.py)"""),

    ("py: bare exception no traceback", "ZeroDivisionError: division by zero"),

    ("py: pytest output", """=================================== FAILURES ===================================
______________________________ test_user_creation ______________________________
    def test_user_creation():
>       assert user.id == 1
E       AttributeError: 'NoneType' object has no attribute 'id'

tests/test_user.py:14: AttributeError
=========================== short test summary info ============================
FAILED tests/test_user.py::test_user_creation - AttributeError: 'NoneType' obj"""),

    # ------------------------------------------------------------- k8s ---
    ("k8s: full describe output", """Name:         payments-api-5d8f9c7b6d-x2k9p
Namespace:    production
Status:       Running
Containers:
  api:
    State:          Waiting
      Reason:       CrashLoopBackOff
    Last State:     Terminated
      Reason:       Error
      Exit Code:    1
      Started:      Sat, 16 Aug 2026 10:20:11 +0000
      Finished:     Sat, 16 Aug 2026 10:20:14 +0000
    Restart Count:  147
Events:
  Type     Reason   Age                    From     Message
  ----     ------   ----                   ----     -------
  Warning  BackOff  2m (x412 over 3h)      kubelet  Back-off restarting failed container"""),

    ("k8s: get pods table", """NAME                            READY   STATUS             RESTARTS   AGE
payments-api-5d8f9c7b6d-x2k9p   0/1     CrashLoopBackOff   147        3h12m"""),

    ("k8s: oom in describe", """    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Sat, 16 Aug 2026 09:02:00 +0000"""),

    ("k8s: image pull with registry port", """Failed to pull image "registry.internal:5000/team/api:v2.1.0": rpc error: code = Unknown desc = failed to pull and unpack image: failed to resolve reference: unexpected status: 401 Unauthorized
  Warning  Failed  3s (x4 over 62s)  kubelet  Error: ErrImagePull"""),

    # ---------------------------------------------------------- docker ---
    ("docker: buildkit failure", """ => ERROR [builder 6/9] RUN pip install -r requirements.txt              12.4s
------
 > [builder 6/9] RUN pip install -r requirements.txt:
0.891 ERROR: Could not find a version that satisfies the requirement torch==2.9.9
------
Dockerfile:14
--------------------
  12 |     COPY requirements.txt .
  14 | >>> RUN pip install -r requirements.txt
--------------------
ERROR: failed to solve: process "/bin/sh -c pip install -r requirements.txt" did not complete successfully: exit code: 1"""),

    ("docker: daemon down", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"),

    # ------------------------------------------------------------ node ---
    ("node: full npm error block", """npm ERR! code ERESOLVE
npm ERR! ERESOLVE unable to resolve dependency tree
npm ERR!
npm ERR! While resolving: my-app@1.0.0
npm ERR! Found: react@18.2.0
npm ERR! node_modules/react
npm ERR!
npm ERR! A complete log of this run can be found in:
npm ERR!     /home/sadi/.npm/_logs/2026-08-16T10_22_31_412Z-debug-0.log"""),

    ("node: js stack trace", """/app/src/index.js:14
  throw new Error('config missing');
  ^

Error: config missing
    at loadConfig (/app/src/index.js:14:9)
    at Object.<anonymous> (/app/src/index.js:22:1)
    at node:internal/main/run_main_module:23:47

Node.js v22.11.0"""),

    # --------------------------------------------------- NOT SUPPORTED ---
    ("go: panic", """panic: runtime error: invalid memory address or nil pointer dereference
[signal SIGSEGV: segmentation violation code=0x1 addr=0x0 pc=0x4a1b2c]

goroutine 1 [running]:
main.process(0x0)
	/app/main.go:42 +0x1c"""),

    ("java: stack trace", """Exception in thread "main" java.lang.NullPointerException: Cannot invoke "String.length()" because "s" is null
	at com.example.App.process(App.java:23)
	at com.example.App.main(App.java:12)"""),

    ("rust: panic", """thread 'main' panicked at src/main.rs:8:27:
called `Option::unwrap()` on a `None` value
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace"""),

    # ------------------------------------------------------- messy real --
    ("ansi colour codes (CI paste)",
     "\x1b[31mERROR\x1b[0m: \x1b[1mModuleNotFoundError\x1b[0m: No module named 'requests'"),

    ("systemd journal prefix",
     "Aug 16 10:22:31 web-01 myapp[1234]: ModuleNotFoundError: No module named 'requests'"),

    ("windows path", r"""Traceback (most recent call last):
  File "C:\Users\sadi\projects\app\main.py", line 12, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'"""),

    ("ruby: NoMethodError on nil", "app/models/user.rb:12:in `full_name': undefined method `upcase' for nil:NilClass (NoMethodError)"),
    ("ruby: LoadError gem", "/usr/lib/ruby/rubygems.rb:275:in `require': cannot load such file -- nokogiri (LoadError)"),
    ("ruby: rails RecordNotFound", "app/jobs/sync.rb:44:in `perform': ActiveRecord::RecordNotFound: Couldn't find User with id=99"),
    ("php: class not found", 'PHP Fatal error:  Uncaught Error: Class "App\\Models\\User" not found in /var/www/html/index.php:14'),
    ("php: undefined function", "PHP Fatal error:  Uncaught Error: Call to undefined function mysqli_connect() in /var/www/app.php:8"),
    ("php: memory exhausted", "PHP Fatal error:  Allowed memory size of 134217728 bytes exhausted in /var/www/big.php on line 22"),
    ("java: ClassNotFound", "Exception in thread \"main\" java.lang.ClassNotFoundException: org.postgresql.Driver\n\tat java.base/java.net.URLClassLoader.findClass(URLClassLoader.java:445)"),
    ("java: OutOfMemory", "Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space\n\tat com.example.Loader.load(Loader.java:88)"),
    ("shell: exited with code 137", "worker-1 exited with code 137"),
    ("shell: make exit status 1", "make: *** [Makefile:14: build] Error 1\ncommand terminated with exit status 1"),
    ("orphan ANSI (esc stripped)", "[31mERROR[0m: [1mModuleNotFoundError[0m: No module named 'requests'"),
    ("shell: generic", "bash: kubectl: command not found"),
    ("net: refused", "curl: (7) Failed to connect to localhost port 8080 after 0 ms: Connection refused"),

    # ---------------------------------------------------------------- git ---
    ("git: SSH permission denied (publickey)", """git@github.com: Permission denied (publickey).
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists."""),

    (
        "git: push rejected, non-fast-forward",
        # approx — hint block wording reconstructed from memory; verify
        # against real git output before trusting this as ground truth
        """To github.com:sadishihab/error-journal.git
 ! [rejected]        main -> main (fetch first)
error: failed to push some refs to 'github.com:sadishihab/error-journal.git'
hint: Updates were rejected because the remote contains work that you do
hint: not have locally. This is usually caused by another repository pushing
hint: to the same ref. You may want to first integrate the remote changes
hint: (e.g., 'git pull ...') before pushing again.
hint: See the 'Note about fast-forwards' in 'git push --help' for details.""",
    ),

    ("git: merge conflict", """Auto-merging src/app.py
CONFLICT (content): Merge conflict in src/app.py
Automatic merge failed; fix conflicts and then commit the result."""),

    ("git: no upstream branch", """fatal: The current branch feature/error-journal-git has no upstream branch.
To push the current branch and set the remote as upstream, use

    git push --set-upstream origin feature/error-journal-git"""),

    # ------------------------------------------------------------ database ---
    ("db: psycopg2 connection refused", """Traceback (most recent call last):
  File "/app/db.py", line 12, in connect
    conn = psycopg2.connect(dsn)
psycopg2.OperationalError: connection to server at "localhost" (127.0.0.1), port 5432 failed: Connection refused
\tIs the server running on that host and accepting TCP/IP connections?"""),

    ("db: psql password authentication failed",
     'psql: error: connection to server at "localhost" (127.0.0.1), port 5432 failed: FATAL:  password authentication failed for user "app_user"'),

    ("db: postgres too many clients",
     "psycopg2.OperationalError: FATAL:  sorry, too many clients already"),

    ("db: mysql access denied",
     "ERROR 1045 (28000): Access denied for user 'app'@'10.0.0.5' (using password: YES)"),

    ("db: mysql cannot connect via socket",
     "ERROR 2002 (HY000): Can't connect to local MySQL server through socket '/var/run/mysqld/mysqld.sock' (2)"),

    # ------------------------------------------------------------- systemd ---
    ("systemd: job failed to start", """Job for nginx.service failed because the control process exited with error code.
See "systemctl status nginx.service" and "journalctl -xeu nginx.service" for details."""),

    (
        "systemd: unit not found",
        # approx — exact wording reconstructed from memory; verify against
        # real systemctl output before trusting this as ground truth
        "Failed to start foo.service: Unit foo.service not found.",
    ),

    ("systemd: journal shows non-zero exit", """Aug 23 09:14:02 web-01 systemd[1]: myapp.service: Main process exited, code=exited, status=1/FAILURE
Aug 23 09:14:02 web-01 systemd[1]: myapp.service: Failed with result 'exit-code'."""),

    # ---------------------------------------------------------------- build ---
    ("build: webpack cannot resolve module", """ERROR in ./src/App.js
Module not found: Error: Can't resolve './components/Header' in '/app/src'
 @ ./src/index.js 5:0-45"""),

    (
        "build: vite failed to resolve import",
        # approx — exact prefix wording reconstructed from memory; verify
        # against real vite output before trusting this as ground truth
        'Failed to resolve import "./components/Header" from "src/App.jsx". Does the file exist?',
    ),

    ("build: tsc TS2307 module not found", """src/api/client.ts:3:22 - error TS2307: Cannot find module './types' or its corresponding type declarations.

3 import { User } from './types';
                       ~~~~~~~~~"""),
]


def main():
    print(f"{'sample':44} {'category':34} match")
    print("-" * 96)
    unmatched = []
    for label, raw in SAMPLES:
        fp = fingerprint(raw)
        flag = "ok" if fp.matched else "MISS"
        if not fp.matched:
            unmatched.append(label)
        print(f"{label:44} {fp.category:34} {flag}")
        print(f"{'':44} tmpl: {fp.template[:70]}")
        if fp.identity:
            print(f"{'':44} idnt: {fp.identity}")
    print("-" * 96)
    print(f"unmatched: {len(unmatched)}/{len(SAMPLES)}")
    for u in unmatched:
        print("   -", u)


if __name__ == "__main__":
    main()
