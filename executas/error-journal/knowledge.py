"""Deterministic diagnosis knowledge base, keyed by fingerprint category.

This is the part a generic chat cannot reliably reproduce: the same correct
answer every time, improvable once for every future user.

Adding a category: keep fix_steps concrete and runnable. Prefer the command
people actually forget over the obvious one. Flag destructive steps inline.
"""

KB = {
    # ---------------------------------------------------------------- k8s ---
    "k8s.crashloop": {
        "severity": "high",
        "root_cause": (
            "The container starts, exits, and is restarted repeatedly. The exit "
            "reason is in the PREVIOUS container's logs, not the current ones."
        ),
        "fix_steps": [
            "kubectl logs <pod> --previous          # logs from the crashed instance",
            "kubectl describe pod <pod>             # check Last State + Exit Code",
            "Exit 1 -> app error; Exit 137 -> OOMKilled; Exit 143 -> SIGTERM",
            "Verify command/args and that required env vars are actually set",
        ],
        "verify_command": "kubectl get pod <pod> -w",
        "confidence": 0.9,
    },
    "k8s.oom": {
        "severity": "high",
        "root_cause": (
            "The container exceeded its memory limit and was killed by the "
            "kernel OOM killer (exit code 137)."
        ),
        "fix_steps": [
            "kubectl describe pod <pod> | grep -A5 Limits",
            "Raise resources.limits.memory, or fix the leak upstream",
            "If limit == request, decoupling them allows burst headroom",
            "Check for unbounded caches or large in-memory reads",
        ],
        "verify_command": "kubectl top pod <pod>",
        "confidence": 0.9,
    },
    "k8s.image_pull": {
        "severity": "high",
        "root_cause": (
            "The kubelet cannot pull the image: the tag does not exist, the "
            "registry is unreachable, or credentials are missing."
        ),
        "fix_steps": [
            "kubectl describe pod <pod> | tail -20   # exact pull error",
            "Confirm the tag exists: docker manifest inspect <image>",
            "Private registry -> check imagePullSecrets is set on the pod spec",
            "Watch for a typo'd registry host or a missing namespace segment",
        ],
        "verify_command": "kubectl describe pod <pod> | grep -i image",
        "confidence": 0.9,
    },
    "k8s.scheduling": {
        "severity": "medium",
        "root_cause": "No node satisfies the pod's resource requests, affinity, or taints.",
        "fix_steps": [
            "kubectl describe pod <pod> | grep -A10 Events",
            "kubectl describe nodes | grep -A5 'Allocated resources'",
            "Lower resources.requests, or add capacity",
            "Check nodeSelector / affinity / tolerations against real node labels",
        ],
        "verify_command": "kubectl get pod <pod> -o wide",
        "confidence": 0.85,
    },
    "k8s.config": {
        "severity": "medium",
        "root_cause": "A referenced ConfigMap or Secret key is missing or misnamed.",
        "fix_steps": [
            "kubectl describe pod <pod> | grep -A5 Events   # names the missing key",
            "kubectl get configmap,secret -n <ns>",
            "Confirm key names match exactly — they are case-sensitive",
        ],
        "verify_command": "kubectl get pod <pod>",
        "confidence": 0.85,
    },
    "k8s.probe": {
        "severity": "medium",
        "root_cause": (
            "The readiness/liveness probe is failing: wrong path or port, or the "
            "app needs longer to warm up than initialDelaySeconds allows."
        ),
        "fix_steps": [
            "kubectl describe pod <pod> | grep -A3 Probe",
            "Test the endpoint from inside: kubectl exec <pod> -- curl -sv localhost:<port><path>",
            "Slow starts -> raise initialDelaySeconds or add a startupProbe",
            "A failing LIVENESS probe restarts the container — that can look like a crashloop",
        ],
        "verify_command": "kubectl get pod <pod> -o wide",
        "confidence": 0.85,
    },
    "k8s.evicted": {
        "severity": "high",
        "root_cause": "The node came under resource pressure and evicted the pod.",
        "fix_steps": [
            "kubectl describe node <node> | grep -A5 Conditions   # Disk/Memory pressure",
            "kubectl get pods --field-selector status.phase=Failed",
            "Set resource requests so the scheduler accounts for the pod properly",
        ],
        "verify_command": "kubectl describe node <node> | grep Pressure",
        "confidence": 0.85,
    },
    # ------------------------------------------------------------- python ---
    "python.module_not_found_error": {
        "severity": "medium",
        "root_cause": "The interpreter cannot find the module on sys.path.",
        "fix_steps": [
            "python -c 'import sys; print(sys.executable)'   # which interpreter is this?",
            "Install into THAT environment, not a different one",
            "Virtualenv active but package missing -> pip install <module>",
            "Local module -> check for a missing __init__.py or a wrong cwd",
        ],
        "verify_command": "python -c 'import <module>; print(<module>.__file__)'",
        "confidence": 0.9,
    },
    "python.import_error": {
        "severity": "medium",
        "root_cause": (
            "The module exists but a name inside it could not be imported — "
            "usually a version mismatch or a circular import."
        ),
        "fix_steps": [
            "pip show <package>            # compare installed vs expected version",
            "Circular import -> move the import inside the function that needs it",
            "Check the name still exists in the installed version's API",
        ],
        "verify_command": "pip show <package>",
        "confidence": 0.8,
    },
    "python.key_error": {
        "severity": "low",
        "root_cause": "A dict lookup used a key that is not present.",
        "fix_steps": [
            "Print the actual keys at the failure point: print(sorted(d.keys()))",
            "Use d.get(key, default) where absence is legitimate",
            "External data (JSON/API) -> validate the shape before access",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "python.attribute_error": {
        "severity": "low",
        "root_cause": (
            "The object does not have the attribute — often it is None, or a "
            "different type than expected."
        ),
        "fix_steps": [
            "Print type(obj) at the failure point — None is the usual culprit",
            "Check whether an earlier call returned None instead of an object",
            "Typo or renamed API -> compare against the installed version's docs",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "python.file_not_found_error": {
        "severity": "low",
        "root_cause": "The path does not exist relative to the process's working directory.",
        "fix_steps": [
            "python -c 'import os; print(os.getcwd())'   # cwd is often not what you expect",
            "Prefer paths built from __file__ over relative literals",
            "In containers, confirm the file was actually COPYed into the image",
        ],
        "verify_command": "ls -la <path>",
        "confidence": 0.85,
    },
    # ------------------------------------------------------------- docker ---
    "docker.port_conflict": {
        "severity": "low",
        "root_cause": "The host port is already bound by another process or container.",
        "fix_steps": [
            "ss -ltnp | grep <port>            # find the holder",
            "docker ps --filter publish=<port>",
            "Stop the conflicting container, or publish a different host port",
        ],
        "verify_command": "ss -ltnp | grep <port>",
        "confidence": 0.95,
    },
    "docker.daemon": {
        "severity": "medium",
        "root_cause": "The Docker daemon is not running, or its socket is not accessible.",
        "fix_steps": [
            "systemctl status docker",
            "sudo systemctl start docker",
            "Permission denied on the socket -> add your user to the docker group, then re-login",
        ],
        "verify_command": "docker info",
        "confidence": 0.9,
    },
    "docker.build": {
        "severity": "medium",
        "root_cause": "A build step exited non-zero. The failing RUN line is named in the output.",
        "fix_steps": [
            "Read the last '=> ERROR' block — it names the exact failing step",
            "Reproduce interactively: docker run -it <last-good-layer> sh",
            "Cache masking a fix? Rebuild with --no-cache to confirm",
        ],
        "verify_command": "docker build --progress=plain .",
        "confidence": 0.75,
    },
    "docker.auth": {
        "severity": "medium",
        "root_cause": "Not authenticated to the registry, or the repository does not exist.",
        "fix_steps": [
            "docker login <registry>",
            "Confirm the full path includes the namespace: <registry>/<org>/<image>",
            "Private repo -> verify the account actually has pull access",
        ],
        "verify_command": "docker pull <image>",
        "confidence": 0.9,
    },
    "docker.image_missing": {
        "severity": "medium",
        "root_cause": "The requested image or tag does not exist in the registry.",
        "fix_steps": [
            "docker manifest inspect <image>    # does this tag exist at all?",
            "Check for a typo in the tag; 'latest' may not be published",
            "Multi-arch host -> confirm a build exists for your platform",
        ],
        "verify_command": "docker manifest inspect <image>",
        "confidence": 0.9,
    },
    "docker.disk": {
        "severity": "high",
        "root_cause": "The filesystem backing Docker has no free space.",
        "fix_steps": [
            "df -h /var/lib/docker",
            "docker system df                  # see what is consuming space",
            "docker system prune -a --volumes  # DESTRUCTIVE — read it before running",
        ],
        "verify_command": "df -h /var/lib/docker",
        "confidence": 0.95,
    },
    # --------------------------------------------------------------- node ---
    "node.module_not_found": {
        "severity": "medium",
        "root_cause": "Node cannot resolve the module from node_modules.",
        "fix_steps": [
            "npm install                       # ensure deps are installed",
            "Confirm it is in dependencies, not just devDependencies",
            "Stale tree -> rm -rf node_modules package-lock.json && npm install",
        ],
        "verify_command": "node -e \"require.resolve('<module>')\"",
        "confidence": 0.85,
    },
    "node.npm_enoent": {
        "severity": "low",
        "root_cause": "npm cannot find a file it expects — usually package.json or a script path.",
        "fix_steps": [
            "ls package.json                   # are you in the right directory?",
            "Check the script name exists under 'scripts' in package.json",
        ],
        "verify_command": "npm run",
        "confidence": 0.85,
    },
    "node.npm_elifecycle": {
        "severity": "medium",
        "root_cause": "A package script exited non-zero. The real error is above this line.",
        "fix_steps": [
            "Scroll UP — ELIFECYCLE is the wrapper, not the cause",
            "Run the underlying script directly to see clean output",
        ],
        "verify_command": None,
        "confidence": 0.7,
    },
    "node.npm_eresolve": {
        "severity": "medium",
        "root_cause": "npm cannot satisfy conflicting peer dependency ranges.",
        "fix_steps": [
            "npm ls <package>                  # see who requires what",
            "Align the conflicting versions properly if you can",
            "--legacy-peer-deps works but defers the problem rather than fixing it",
        ],
        "verify_command": "npm ls <package>",
        "confidence": 0.8,
    },
    "node.npm_eacces": {
        "severity": "medium",
        "root_cause": "npm lacks permission to write to its target directory.",
        "fix_steps": [
            "Avoid sudo npm — it creates root-owned files that break later installs",
            "Set a user-level global prefix: npm config set prefix ~/.npm-global",
            "Or manage Node via nvm, which sidesteps this entirely",
        ],
        "verify_command": "npm config get prefix",
        "confidence": 0.85,
    },
    # -------------------------------------------------------- net / shell ---
    "net.connection_refused": {
        "severity": "medium",
        "root_cause": "Nothing is listening on the target host:port, or a firewall rejected it.",
        "fix_steps": [
            "Confirm the service is up and bound to the expected interface",
            "Bound to 127.0.0.1 will refuse external connections — bind 0.0.0.0",
            "In k8s, check the Service selector actually matches pod labels",
        ],
        "verify_command": "curl -v <host>:<port>",
        "confidence": 0.8,
    },
    "net.timeout": {
        "severity": "medium",
        "root_cause": "The connection was never answered — typically a firewall drop or wrong host.",
        "fix_steps": [
            "Refused vs timed out matters: refused = reachable, timed out = dropped",
            "Check security groups / NetworkPolicy / egress rules",
            "Confirm the host resolves to the address you expect",
        ],
        "verify_command": "nc -vz <host> <port>",
        "confidence": 0.75,
    },
    "net.dns": {
        "severity": "medium",
        "root_cause": "The hostname could not be resolved.",
        "fix_steps": [
            "getent hosts <host>",
            "In k8s, use the full form: <svc>.<namespace>.svc.cluster.local",
            "Check CoreDNS is healthy: kubectl -n kube-system get pods -l k8s-app=kube-dns",
        ],
        "verify_command": "getent hosts <host>",
        "confidence": 0.8,
    },
    # ------------------------------------------------------- go/java/rust ---
    "go.nil_pointer": {
        "severity": "high",
        "root_cause": "A nil pointer was dereferenced. The panic trace names the exact line.",
        "fix_steps": [
            "Read the first goroutine frame — that is your code, not the runtime",
            "A function returned (nil, err) and the err was not checked",
            "Guard the pointer before use, or fix the caller that ignored the error",
        ],
        "verify_command": "go run -race .",
        "confidence": 0.85,
    },
    "go.index_out_of_range": {
        "severity": "medium",
        "root_cause": "A slice or array was indexed beyond its length.",
        "fix_steps": [
            "The panic prints both index and length — compare them",
            "Check loop bounds and any len() assumption made before a slice op",
            "Slicing an empty result set is the usual cause",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "go.panic": {
        "severity": "high",
        "root_cause": "The program panicked and unwound the stack.",
        "fix_steps": [
            "The first goroutine frame is the origin",
            "Recover only at a boundary you control; do not swallow panics broadly",
        ],
        "verify_command": None,
        "confidence": 0.6,
    },
    "java.null_pointer_exception": {
        "severity": "medium",
        "root_cause": "A method or field was accessed on a null reference.",
        "fix_steps": [
            "Java 14+ prints the exact expression that was null — read the message closely",
            "Trace back to where the value should have been assigned",
            "Prefer Optional or an explicit null check at the boundary",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "rust.unwrap_none": {
        "severity": "medium",
        "root_cause": "unwrap() was called on an Option that was None.",
        "fix_steps": [
            "Replace unwrap() with a match, if let, or unwrap_or_else",
            "Use expect(\"why this should exist\") so the next panic explains itself",
            "RUST_BACKTRACE=1 gives the full frame list",
        ],
        "verify_command": "RUST_BACKTRACE=1 cargo run",
        "confidence": 0.85,
    },
    "rust.index_out_of_bounds": {
        "severity": "medium",
        "root_cause": "A slice or Vec was indexed past its length.",
        "fix_steps": [
            "The panic prints the index and the length",
            "Prefer .get(i) which returns Option instead of panicking",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "java.class_not_found_exception": {
        "severity": "high",
        "root_cause": "A class named at runtime is not on the classpath.",
        "fix_steps": [
            "Confirm the dependency is declared in pom.xml / build.gradle",
            "mvn dependency:tree | grep <artifact>   — is it actually resolved?",
            "Provided/compileOnly scope means it is absent at runtime — check the scope",
            "Fat-jar builds: verify the shade/shadow plugin included it",
        ],
        "verify_command": "mvn dependency:tree",
        "confidence": 0.85,
    },
    "java.no_class_def_found_error": {
        "severity": "high",
        "root_cause": (
            "The class was present at compile time but is missing at runtime — "
            "or its static initialiser threw."
        ),
        "fix_steps": [
            "Different from ClassNotFoundException: it compiled, so this is a runtime gap",
            "Check for a version mismatch between compile and runtime classpaths",
            "Look further up the log for an ExceptionInInitializerError",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "java.out_of_memory_error": {
        "severity": "high",
        "root_cause": "The JVM heap (or metaspace) was exhausted.",
        "fix_steps": [
            "Read which space failed: 'Java heap space' vs 'Metaspace' differ",
            "Raise it: -Xmx2g (heap) or -XX:MaxMetaspaceSize (metaspace)",
            "In containers, prefer -XX:MaxRAMPercentage over a fixed -Xmx",
            "Repeated OOM after a raise means a leak — take a heap dump",
        ],
        "verify_command": "jcmd <pid> GC.heap_info",
        "confidence": 0.85,
    },
    "java.sql_exception": {
        "severity": "high",
        "root_cause": "The database rejected the connection or the statement.",
        "fix_steps": [
            "The vendor error code in the message is the real detail — read it",
            "Connection refused -> host/port/firewall; auth failed -> credentials",
            "'No suitable driver' means the JDBC driver is not on the classpath",
        ],
        "verify_command": None,
        "confidence": 0.75,
    },
    # ------------------------------------------------------------- ruby ---
    "ruby.no_method_error": {
        "severity": "medium",
        "root_cause": (
            "A method was called on an object that does not define it — "
            "very often the receiver is nil."
        ),
        "fix_steps": [
            "'for nil:NilClass' means the receiver was nil, not that the method is missing",
            "Trace back to where the value should have been set",
            "Use &. for safe navigation where nil is legitimate",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "ruby.load_error": {
        "severity": "medium",
        "root_cause": "A required file or gem could not be found on the load path.",
        "fix_steps": [
            "bundle install                     # is the gem actually installed?",
            "Confirm it is in the Gemfile, and run under bundle exec",
            "Native-extension gems can fail to build silently — reinstall and read the output",
        ],
        "verify_command": "bundle exec ruby -e \"require '<lib>'\"",
        "confidence": 0.85,
    },
    "ruby.name_error": {
        "severity": "low",
        "root_cause": "An undefined local variable or constant was referenced.",
        "fix_steps": [
            "Usually a typo, or a variable defined in a different scope",
            "For constants, check the class is required/autoloaded",
            "In Rails, a naming mismatch breaks autoloading — file name must match the class",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "ruby.record_not_found": {
        "severity": "low",
        "root_cause": "An ActiveRecord lookup found no matching row.",
        "fix_steps": [
            "find raises; find_by returns nil — pick the one matching your intent",
            "Confirm the id exists and is not scoped away by a default_scope",
            "In controllers, rescue_from and return 404 rather than a 500",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    # -------------------------------------------------------------- php ---
    "php.class_not_found": {
        "severity": "high",
        "root_cause": "The autoloader could not resolve the class.",
        "fix_steps": [
            "composer dump-autoload",
            "Check the namespace matches the PSR-4 path in composer.json exactly",
            "Case matters on Linux even though it may work on macOS",
        ],
        "verify_command": "composer dump-autoload -o",
        "confidence": 0.85,
    },
    "php.undefined_function": {
        "severity": "medium",
        "root_cause": "A PHP extension providing the function is not installed or enabled.",
        "fix_steps": [
            "php -m | grep <ext>              # is the extension loaded?",
            "Install it: apt install php-<ext>  (or docker-php-ext-install <ext>)",
            "Restart php-fpm after enabling — the CLI and FPM load separate ini files",
        ],
        "verify_command": "php -m",
        "confidence": 0.9,
    },
    "php.memory_exhausted": {
        "severity": "high",
        "root_cause": "The script exceeded memory_limit.",
        "fix_steps": [
            "Raise memory_limit in php.ini, or ini_set for one script",
            "Loading a large result set at once is the usual cause — chunk it",
            "-1 means unlimited; use it only for CLI, never for web",
        ],
        "verify_command": "php -i | grep memory_limit",
        "confidence": 0.85,
    },
    "php.file_not_found": {
        "severity": "medium",
        "root_cause": "A file operation targeted a path that does not exist or is unreadable.",
        "fix_steps": [
            "Check the path relative to the script, not the cwd",
            "Confirm the web server user can read it (www-data, nginx)",
            "open_basedir restrictions can block otherwise-valid paths",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "shell.command_not_found": {
        "severity": "low",
        "root_cause": "The binary is not installed, or not on PATH for this shell.",
        "fix_steps": [
            "which <cmd> ; echo $PATH",
            "Installed but not found -> its bin dir is missing from PATH",
            "In containers, confirm it was installed in the final build stage",
        ],
        "verify_command": "which <cmd>",
        "confidence": 0.9,
    },
    "shell.permission": {
        "severity": "low",
        "root_cause": "The current user lacks permission on the target path or socket.",
        "fix_steps": [
            "ls -la <path>                     # check owner and mode",
            "id                                # check your groups",
            "Prefer fixing group membership over chmod 777",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "shell.missing_path": {
        "severity": "low",
        "root_cause": "The path does not exist from the current working directory.",
        "fix_steps": [
            "pwd ; ls -la                      # confirm where you actually are",
            "Check for a typo or an unexpanded variable in the path",
        ],
        "verify_command": "ls -la <path>",
        "confidence": 0.85,
    },
}

UNKNOWN = {
    "severity": "unknown",
    "root_cause": None,
    "fix_steps": [],
    "verify_command": None,
    "confidence": 0.0,
}
