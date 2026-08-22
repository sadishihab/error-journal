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
    # --------------------------------------- high-frequency: javascript ---
    "node.type_error": {
        "severity": "medium",
        "root_cause": (
            "A property or method was accessed on undefined or null. The value "
            "is almost never wrong at the point it blew up — it was wrong earlier."
        ),
        "fix_steps": [
            "The message names the property: 'reading X' means the object holding X was undefined",
            "Trace back to what should have produced that object \u2014 a failed fetch, a missing key, an unawaited promise",
            "An unawaited async call returns a Promise, not the value \u2014 check for a missing await",
            "Use optional chaining (obj?.prop) only where absence is genuinely valid",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "node.reference_error": {
        "severity": "medium",
        "root_cause": "A variable was used before it was defined or imported.",
        "fix_steps": [
            "'X is not defined' \u2014 check for a typo or a missing import",
            "In ESM, `require` and `__dirname` do not exist; in CJS, top-level `import` does not",
            "Browser globals (window, document) are absent in Node and vice versa",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "node.syntax_error": {
        "severity": "medium",
        "root_cause": "The file could not be parsed.",
        "fix_steps": [
            "'Unexpected token' usually points at the line AFTER the real mistake",
            "'Cannot use import statement outside a module' -> set \"type\": \"module\" in package.json, or rename to .mjs",
            "Unexpected token in JSON means the response was HTML \u2014 usually an error page, not JSON",
        ],
        "verify_command": "node --check <file>",
        "confidence": 0.85,
    },
    "node.unhandled_rejection": {
        "severity": "high",
        "root_cause": "A promise rejected with no catch handler. Node exits on this by default.",
        "fix_steps": [
            "Add .catch() or wrap the await in try/catch at the boundary",
            "Any async function called without await swallows its own errors \u2014 find it",
            "Register process.on('unhandledRejection') to log rather than exit silently",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    # ------------------------------------------- high-frequency: python ---
    "python.type_error": {
        "severity": "medium",
        "root_cause": "An operation received an object of the wrong type.",
        "fix_steps": [
            "'NoneType' in the message means a function returned None where a value was expected",
            "Read the message closely \u2014 it names both types involved",
            "'takes N positional arguments but M were given' on a method usually means a missing self",
            "Print type(x) at the failure point rather than guessing",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "python.value_error": {
        "severity": "medium",
        "root_cause": "The type was right but the value was not acceptable.",
        "fix_steps": [
            "int('') and int('12a') raise this \u2014 validate before converting",
            "'not enough values to unpack' means the right-hand side had a different length than expected",
            "For external input, validate at the boundary rather than deep in the call stack",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "python.index_error": {
        "severity": "low",
        "root_cause": "A sequence was indexed beyond its length.",
        "fix_steps": [
            "Check len() before indexing, especially on a filtered or query result",
            "An empty list from a query that returned nothing is the usual cause",
            "Slicing (lst[:1]) does not raise; indexing (lst[0]) does",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "python.connection_error": {
        "severity": "medium",
        "root_cause": "An outbound HTTP or socket connection failed.",
        "fix_steps": [
            "Confirm the host resolves and the port is open: nc -vz <host> <port>",
            "Inside a container, localhost is the container \u2014 use the service name or host.docker.internal",
            "Set explicit timeouts; requests has none by default and will hang",
        ],
        "verify_command": "curl -v <url>",
        "confidence": 0.8,
    },
    "python.permission_error": {
        "severity": "medium",
        "root_cause": "The process lacks permission on the target path.",
        "fix_steps": [
            "ls -la <path> and id \u2014 compare owner/mode against your user",
            "In containers, the app often runs as a non-root UID that does not own the mounted volume",
            "Prefer fixing ownership over chmod 777",
        ],
        "verify_command": "ls -la <path>",
        "confidence": 0.85,
    },
    "python.json_decode_error": {
        "severity": "low",
        "root_cause": "The text being parsed was not valid JSON.",
        "fix_steps": [
            "Print the first 200 characters of the raw body \u2014 it is usually an HTML error page",
            "'Expecting value: line 1 column 1' means the body was empty or not JSON at all",
            "Check response.status_code before parsing",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "python.runtime_error": {
        "severity": "medium",
        "root_cause": "A generic runtime failure. The message carries the real detail.",
        "fix_steps": [
            "Read the message itself \u2014 RuntimeError is a catch-all, so the text is the diagnosis",
            "In async code, 'no running event loop' means a coroutine was called outside asyncio.run",
            "In PyTorch, CUDA-related RuntimeErrors usually mean a device or shape mismatch",
        ],
        "verify_command": None,
        "confidence": 0.5,
    },
    "python.zero_division_error": {
        "severity": "low",
        "root_cause": "A division or modulo used zero as the divisor.",
        "fix_steps": [
            "Guard the denominator \u2014 an empty collection giving len() == 0 is the usual cause",
            "For averages and rates, return 0 or None explicitly when there is no data",
        ],
        "verify_command": None,
        "confidence": 0.9,
    },
    # ---------------------------------------------- high-frequency: k8s ---
    "k8s.image_pull_secret": {
        "severity": "high",
        "root_cause": "The pod has no valid credentials for a private registry.",
        "fix_steps": [
            "kubectl get secret <name> -n <ns>   \u2014 does it exist in the POD's namespace?",
            "Secrets are namespaced; one in default will not work in production",
            "imagePullSecrets must be on the pod spec (or the ServiceAccount), not the deployment metadata",
        ],
        "verify_command": "kubectl get sa default -n <ns> -o yaml",
        "confidence": 0.85,
    },
    # -------------------------------------------- high-frequency: shell ---
    "shell.exit_137": {
        "severity": "high",
        "root_cause": "The process was killed by SIGKILL \u2014 almost always the OOM killer.",
        "fix_steps": [
            "137 = 128 + 9 (SIGKILL). In containers this means the memory limit was hit",
            "dmesg | grep -i 'killed process'   \u2014 confirms an OOM kill",
            "Raise the memory limit, or reduce peak usage",
        ],
        "verify_command": "dmesg | tail -20",
        "confidence": 0.85,
    },
    "shell.exit_1": {
        "severity": "low",
        "root_cause": "A generic non-zero exit. The real error is earlier in the output.",
        "fix_steps": [
            "Exit 1 is the wrapper, not the cause \u2014 scroll up for the actual message",
            "Run the failing command directly to see clean output",
        ],
        "verify_command": None,
        "confidence": 0.4,
    },
    "python.json_decode_error": {
        "severity": "low",
        "root_cause": "The text being parsed was not valid JSON.",
        "fix_steps": [
            "Print the first 200 characters of the raw body \u2014 it is usually an HTML error page",
            "'Expecting value: line 1 column 1' means the body was empty or not JSON at all",
            "Check response.status_code before parsing",
        ],
        "verify_command": None,
        "confidence": 0.85,
    },
    "python.os_error": {
        "severity": "medium",
        "root_cause": "A system call failed. The errno in the message is the real detail.",
        "fix_steps": [
            "Errno 28 = disk full; 24 = too many open files; 98 = address already in use",
            "'Too many open files' means unclosed handles \u2014 use context managers, or raise ulimit -n",
            "'Address already in use' means the port is held; find it with ss -ltnp",
        ],
        "verify_command": None,
        "confidence": 0.7,
    },
    "python.http_error": {
        "severity": "medium",
        "root_cause": "The server returned a 4xx or 5xx status.",
        "fix_steps": [
            "4xx is your request (auth, path, payload); 5xx is theirs \u2014 retry with backoff",
            "401/403 -> credentials or scope; 404 -> path or resource id; 429 -> rate limited",
            "Log response.text, not just the status \u2014 the body usually explains why",
        ],
        "verify_command": "curl -i <url>",
        "confidence": 0.75,
    },
    "python.ssl_error": {
        "severity": "medium",
        "root_cause": "TLS verification or negotiation failed.",
        "fix_steps": [
            "'certificate verify failed' -> missing CA bundle, or a self-signed cert",
            "In slim containers, install ca-certificates \u2014 it is often absent",
            "Do not disable verification to make it pass; fix the trust chain",
        ],
        "verify_command": "openssl s_client -connect <host>:443",
        "confidence": 0.8,
    },
    # --------------------------------------------------------------- git ---
    "git.auth_publickey": {
        "severity": "medium",
        "root_cause": "SSH could not authenticate to the remote — no key offered, or none accepted.",
        "fix_steps": [
            "ssh -T git@github.com          # does the host recognise you at all?",
            "ssh-add -l                     # is a key loaded in the agent?",
            "If empty: ssh-add ~/.ssh/id_ed25519",
            "Key must be mode 600 — SSH silently ignores world-readable keys",
            "Confirm the PUBLIC key is added to the account, not the private one",
        ],
        "verify_command": "ssh -T git@github.com",
        "confidence": 0.9,
    },
    "git.push_rejected_behind": {
        "severity": "low",
        "root_cause": "The remote has commits you do not have locally, so the push would lose them.",
        "fix_steps": [
            "git pull --rebase              # replay your work on top of theirs",
            "Resolve any conflicts, then: git rebase --continue",
            "git push",
            "Do NOT reach for --force: it deletes the commits you were warned about. "
            "If you genuinely must overwrite, use --force-with-lease, which refuses "
            "if someone pushed after your last fetch.",
        ],
        "verify_command": "git log --oneline origin/main..HEAD",
        "confidence": 0.9,
    },
    "git.push_rejected": {
        "severity": "low",
        "root_cause": "The remote refused the push. The hint lines above name the specific reason.",
        "fix_steps": [
            "git fetch && git status        # see how the branches relate",
            "Protected branch? Open a pull request instead of pushing directly",
            "Non-fast-forward? git pull --rebase first",
        ],
        "verify_command": "git status -sb",
        "confidence": 0.7,
    },
    "git.merge_conflict": {
        "severity": "low",
        "root_cause": "The same lines changed on both sides, so git cannot merge automatically.",
        "fix_steps": [
            "git status                     # lists every conflicted file",
            "Edit each file: remove <<<<<<<, =======, >>>>>>> and keep what you want",
            "git add <file> for each resolved file, then git commit",
            "Wrong turn? git merge --abort returns you to before the merge",
            "Prefer resolving in a diff tool over hand-editing markers: git mergetool",
        ],
        "verify_command": "git diff --check",
        "confidence": 0.9,
    },
    "git.index_lock": {
        "severity": "low",
        "root_cause": (
            "Another git process holds the lock — or one crashed and left it behind."
        ),
        "fix_steps": [
            "Check nothing is actually running: ps aux | grep git",
            "An editor or IDE running git in the background is the usual culprit",
            "Only if nothing is running: rm -f .git/index.lock",
            "Deleting the lock while a real process holds it can corrupt the index",
        ],
        "verify_command": "git status",
        "confidence": 0.9,
    },
    "git.detached_head": {
        "severity": "low",
        "root_cause": (
            "HEAD points at a commit rather than a branch. Commits made here belong "
            "to no branch and are easy to lose."
        ),
        "fix_steps": [
            "Just looking? git switch -    returns you to where you were",
            "Made commits you want to keep: git switch -c <new-branch>",
            "Already switched away and lost them? git reflog finds the commit",
        ],
        "verify_command": "git status",
        "confidence": 0.9,
    },
    "git.no_upstream": {
        "severity": "low",
        "root_cause": "The branch has no remote counterpart, so git does not know where to push.",
        "fix_steps": [
            "git push -u origin HEAD       # pushes and sets upstream in one go",
            "-u only needs doing once per branch",
            "To make this automatic: git config --global push.autoSetupRemote true",
        ],
        "verify_command": "git status -sb",
        "confidence": 0.95,
    },
    "git.not_a_repo": {
        "severity": "low",
        "root_cause": "Neither this directory nor any parent contains a .git directory.",
        "fix_steps": [
            "pwd                            # you are probably not where you think",
            "Starting fresh? git init",
            "Meant to clone? git clone <url> and cd into it",
        ],
        "verify_command": "git rev-parse --show-toplevel",
        "confidence": 0.95,
    },
    "git.local_changes_overwritten": {
        "severity": "medium",
        "root_cause": "The incoming change touches files you have modified but not committed.",
        "fix_steps": [
            "Want to keep them: git stash, then pull, then git stash pop",
            "Want to keep them permanently: commit first, then pull",
            "Genuinely want to discard: git checkout -- <file> — this is unrecoverable",
            "Unsure? git stash is always the safe option; nothing is lost",
        ],
        "verify_command": "git status",
        "confidence": 0.9,
    },
    "git.unrelated_histories": {
        "severity": "medium",
        "root_cause": (
            "The two branches share no common ancestor — usually a local repo being "
            "merged with a remote that was initialised separately."
        ),
        "fix_steps": [
            "Confirm this is what you intend: the histories are genuinely unrelated",
            "git pull --allow-unrelated-histories",
            "Expect conflicts, including on README and .gitignore",
        ],
        "verify_command": "git log --oneline --graph --all | head -20",
        "confidence": 0.85,
    },
    "git.password_auth_removed": {
        "severity": "medium",
        "root_cause": "GitHub stopped accepting account passwords over HTTPS in 2021.",
        "fix_steps": [
            "Use a personal access token as the password, not your account password",
            "Or switch to SSH: git remote set-url origin git@github.com:<owner>/<repo>.git",
            "SSH avoids token expiry entirely — worth doing once",
        ],
        "verify_command": "git remote -v",
        "confidence": 0.95,
    },
    "git.repo_not_found": {
        "severity": "medium",
        "root_cause": (
            "The remote path is wrong, or you lack access. A private repo you cannot "
            "read is reported as not found, deliberately."
        ),
        "fix_steps": [
            "git remote -v                  # check the URL for typos",
            "Private repo? Confirm your account actually has access",
            "SSH key belongs to a different account than you expect? ssh -T git@github.com",
        ],
        "verify_command": "git ls-remote origin",
        "confidence": 0.85,
    },
    # ---------------------------------------------------------- database ---
    "db.pg_auth_failed": {
        "severity": "medium",
        "root_cause": "Postgres rejected the credentials for that role.",
        "fix_steps": [
            "Check the role exists: psql -U postgres -c '\\du'",
            "In containers, the app often reads a different env var than you set "
            "\u2014 print the DSN the app actually used",
            "pg_hba.conf controls WHICH auth method applies per host/user; a 'trust' "
            "line locally and 'md5' remotely explains 'works on my machine'",
            "Special characters in the password must be URL-encoded inside a DSN",
        ],
        "verify_command": "psql \"$DATABASE_URL\" -c 'select 1'",
        "confidence": 0.85,
    },
    "db.pg_database_missing": {
        "severity": "medium",
        "root_cause": "The named database does not exist on that server.",
        "fix_steps": [
            "psql -U postgres -c '\\l'      # list what does exist",
            "Create it: createdb <name>",
            "Postgres names are case-sensitive when quoted \u2014 check for stray quotes",
            "In Docker, POSTGRES_DB only creates the DB on FIRST start; an existing "
            "volume keeps the old one. Remove the volume or create it manually.",
        ],
        "verify_command": "psql -U postgres -lqt | cut -d'|' -f1",
        "confidence": 0.9,
    },
    "db.pg_relation_missing": {
        "severity": "medium",
        "root_cause": "The table or view does not exist \u2014 usually migrations have not run.",
        "fix_steps": [
            "Run your migrations (alembic upgrade head, rails db:migrate, etc.)",
            "\\dt in psql lists tables in the current search_path",
            "Table exists but is not found? Check the schema: it may be in a schema "
            "outside search_path",
            "Connected to the wrong database entirely is the other common cause",
        ],
        "verify_command": "psql \"$DATABASE_URL\" -c '\\dt'",
        "confidence": 0.85,
    },
    "db.pg_too_many_connections": {
        "severity": "high",
        "root_cause": (
            "max_connections is exhausted. Almost always connection leakage or an "
            "over-large pool, not genuine load."
        ),
        "fix_steps": [
            "See who is connected: select count(*), state from pg_stat_activity group by state;",
            "Lots of 'idle in transaction' means code is not closing transactions \u2014 fix that first",
            "Pool size x replica count must stay under max_connections; this is the usual arithmetic error",
            "Put PgBouncer in front rather than raising max_connections \u2014 each connection costs real memory",
        ],
        "verify_command": "psql -c 'select count(*) from pg_stat_activity'",
        "confidence": 0.85,
    },
    "db.pg_deadlock": {
        "severity": "high",
        "root_cause": "Two transactions each hold a lock the other needs. Postgres killed one.",
        "fix_steps": [
            "The log names both statements \u2014 read them, the pattern is usually obvious",
            "Most common fix: acquire locks in a consistent order everywhere",
            "Batch updates sorted by primary key rather than in arbitrary order",
            "Deadlocks are expected under load \u2014 retry the transaction rather than surfacing an error",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "db.pg_connection_refused": {
        "severity": "high",
        "root_cause": "Nothing is listening at that host and port, or the connection was blocked.",
        "fix_steps": [
            "Is Postgres running? pg_isready -h <host> -p 5432",
            "In Docker, 'localhost' means the container itself \u2014 use the service name",
            "listen_addresses defaults to localhost only; remote access needs '*'",
            "pg_hba.conf must also permit the client address",
        ],
        "verify_command": "pg_isready -h <host> -p 5432",
        "confidence": 0.85,
    },
    "db.pg_ssl_required": {
        "severity": "low",
        "root_cause": "The server requires TLS and the client attempted a plaintext connection.",
        "fix_steps": [
            "Append ?sslmode=require to the connection URL",
            "Managed providers (RDS, Cloud SQL, Neon, Supabase) all require this",
            "sslmode=require encrypts but does not verify; use verify-full in production",
        ],
        "verify_command": "psql \"$DATABASE_URL?sslmode=require\" -c 'select 1'",
        "confidence": 0.9,
    },
    "db.mysql_access_denied": {
        "severity": "medium",
        "root_cause": "MySQL rejected that user for that host.",
        "fix_steps": [
            "MySQL grants are per user AND host: 'app'@'localhost' and 'app'@'%' are different users",
            "SELECT user, host FROM mysql.user;   shows what actually exists",
            "'using password: NO' in the message means no password was sent at all",
            "Connecting over TCP from a container needs a '%' or subnet host grant",
        ],
        "verify_command": "mysql -u <user> -p -e 'select 1'",
        "confidence": 0.85,
    },
    "db.mysql_unknown_database": {
        "severity": "medium",
        "root_cause": "The named database does not exist on that server.",
        "fix_steps": [
            "SHOW DATABASES;",
            "CREATE DATABASE <name>;",
            "On Linux, database names are case-sensitive by default",
        ],
        "verify_command": "mysql -e 'show databases'",
        "confidence": 0.9,
    },
    "db.mysql_cant_connect": {
        "severity": "high",
        "root_cause": "The client could not reach the server socket or port.",
        "fix_steps": [
            "systemctl status mysql",
            "'via UNIX socket' means it tried a local socket \u2014 use -h 127.0.0.1 to force TCP",
            "In containers, use the service name, not localhost",
            "bind-address in my.cnf defaults to 127.0.0.1 and blocks remote clients",
        ],
        "verify_command": "mysqladmin ping -h <host>",
        "confidence": 0.85,
    },
    "db.mysql_too_many_connections": {
        "severity": "high",
        "root_cause": "max_connections is exhausted.",
        "fix_steps": [
            "SHOW PROCESSLIST;   shows what is holding connections",
            "Many sleeping connections means the app is not returning them to the pool",
            "Raising max_connections buys time; fixing the leak fixes the problem",
        ],
        "verify_command": "mysql -e 'show status like \"Threads_connected\"'",
        "confidence": 0.85,
    },
    "db.mysql_lock_timeout": {
        "severity": "medium",
        "root_cause": "A transaction waited too long for a row lock held by another.",
        "fix_steps": [
            "SELECT * FROM information_schema.innodb_trx;   finds long-running transactions",
            "Usually a transaction left open by application code \u2014 commit or roll back promptly",
            "Large batch updates hold locks; chunk them",
        ],
        "verify_command": None,
        "confidence": 0.8,
    },
    "db.redis_oom": {
        "severity": "high",
        "root_cause": "Redis hit maxmemory and the eviction policy forbids evicting.",
        "fix_steps": [
            "redis-cli info memory | grep used_memory_human",
            "Default policy is noeviction, which errors rather than dropping keys",
            "Using Redis as a cache? Set maxmemory-policy allkeys-lru",
            "Using it as a store? Raise maxmemory \u2014 evicting would lose data",
            "Check for keys with no TTL: they accumulate forever",
        ],
        "verify_command": "redis-cli info memory",
        "confidence": 0.9,
    },
    "db.redis_auth": {
        "severity": "medium",
        "root_cause": "Redis requires authentication and none, or the wrong credential, was sent.",
        "fix_steps": [
            "NOAUTH means no password sent; WRONGPASS means it was wrong",
            "Include it in the URL: redis://:password@host:6379",
            "Redis 6+ supports usernames (ACLs); older versions use the password only",
        ],
        "verify_command": "redis-cli -a <password> ping",
        "confidence": 0.9,
    },
    "db.redis_readonly": {
        "severity": "high",
        "root_cause": "You are connected to a replica, which rejects writes.",
        "fix_steps": [
            "After a failover the old primary becomes a replica \u2014 clients holding the old address break",
            "Use Sentinel or Cluster discovery rather than a hardcoded host",
            "redis-cli info replication   confirms the role",
        ],
        "verify_command": "redis-cli info replication | grep role",
        "confidence": 0.85,
    },
    "db.redis_connection_refused": {
        "severity": "high",
        "root_cause": "Nothing is listening on that Redis address.",
        "fix_steps": [
            "redis-cli -h <host> ping",
            "In containers, use the service name rather than localhost",
            "Redis binds to 127.0.0.1 by default; remote access needs bind 0.0.0.0 and protected-mode off",
            "Do not expose Redis publicly without a password \u2014 it is scanned for constantly",
        ],
        "verify_command": "redis-cli -h <host> ping",
        "confidence": 0.85,
    },
    "db.mongo_auth_failed": {
        "severity": "medium",
        "root_cause": "MongoDB rejected the credentials.",
        "fix_steps": [
            "Users belong to a specific database \u2014 authSource must match where the user was created",
            "Add ?authSource=admin to the URI if the user lives in admin",
            "URL-encode special characters in the password",
        ],
        "verify_command": "mongosh \"$MONGO_URI\" --eval 'db.runCommand({ping:1})'",
        "confidence": 0.85,
    },
    "db.mongo_server_selection": {
        "severity": "high",
        "root_cause": "The driver could not reach any suitable server before timing out.",
        "fix_steps": [
            "Atlas? The client IP must be in the Network Access allow-list",
            "mongodb+srv:// requires working DNS SRV lookups \u2014 some networks block them",
            "For a replica set, EVERY member hostname must resolve from the client",
        ],
        "verify_command": "mongosh \"$MONGO_URI\" --eval 'db.runCommand({ping:1})'",
        "confidence": 0.8,
    },
    # ----------------------------------------------------------- systemd ---
    "systemd.job_failed": {
        "severity": "high",
        "root_cause": (
            "The unit's main process exited non-zero. systemctl only tells you it "
            "failed \u2014 the reason is in the journal."
        ),
        "fix_steps": [
            "journalctl -u <unit> -n 50 --no-pager    # this is where the real error is",
            "systemctl status <unit> -l               # last few lines plus exit code",
            "Config change? Validate before restarting: nginx -t, sshd -t, etc.",
            "Exit 203/EXEC means the ExecStart binary path is wrong or not executable",
        ],
        "verify_command": "systemctl status <unit>",
        "confidence": 0.85,
    },
    "systemd.exited_nonzero": {
        "severity": "high",
        "root_cause": "The service process exited with a non-zero status.",
        "fix_steps": [
            "journalctl -u <unit> --since '5 min ago'",
            "status=203/EXEC \u2014 ExecStart path wrong or not executable",
            "status=200/CHDIR \u2014 WorkingDirectory does not exist",
            "status=1 \u2014 the application itself failed; its own logs have the reason",
            "Works by hand but not as a service? The unit has a different PATH, user and env",
        ],
        "verify_command": "journalctl -u <unit> -n 50 --no-pager",
        "confidence": 0.85,
    },
    "systemd.unit_not_found": {
        "severity": "medium",
        "root_cause": "No unit file with that name is known to systemd.",
        "fix_steps": [
            "systemctl list-unit-files | grep <name>",
            "New or edited unit file? systemctl daemon-reload",
            "User units need --user; system units need root",
            "Check the filename ends in .service and lives in /etc/systemd/system/",
        ],
        "verify_command": "systemctl list-unit-files | grep <name>",
        "confidence": 0.9,
    },
    "systemd.unit_masked": {
        "severity": "medium",
        "root_cause": (
            "The unit is masked \u2014 symlinked to /dev/null so it cannot start. This is "
            "deliberate, not accidental."
        ),
        "fix_steps": [
            "systemctl unmask <unit>",
            "Find out why it was masked before unmasking \u2014 often a conflicting service",
            "Masking is stronger than disabling: it blocks manual starts too",
        ],
        "verify_command": "systemctl is-enabled <unit>",
        "confidence": 0.9,
    },
    "systemd.restart_loop": {
        "severity": "high",
        "root_cause": (
            "The unit failed repeatedly and systemd gave up under its rate limit. The "
            "restart loop is the symptom; the first failure is the cause."
        ),
        "fix_steps": [
            "journalctl -u <unit> --since '10 min ago'   # scroll to the FIRST failure",
            "systemctl reset-failed <unit>   clears the rate limit so it can start again",
            "Fix the underlying failure before restarting, or it will loop again",
            "StartLimitIntervalSec / StartLimitBurst control the threshold",
        ],
        "verify_command": "systemctl status <unit>",
        "confidence": 0.85,
    },
    "systemd.start_failed": {
        "severity": "high",
        "root_cause": "The unit could not be started.",
        "fix_steps": [
            "journalctl -xeu <unit>    gives the failure with added explanation",
            "Check for a port already in use: ss -ltnp | grep <port>",
            "Check file permissions for the unit's User=",
        ],
        "verify_command": "systemctl status <unit>",
        "confidence": 0.8,
    },
    "systemd.permission": {
        "severity": "low",
        "root_cause": "The operation needs privileges the current user does not have.",
        "fix_steps": [
            "Prefix with sudo",
            "Over SSH without a TTY, polkit cannot prompt \u2014 use sudo explicitly",
            "For a user-level service, add --user and drop sudo",
        ],
        "verify_command": None,
        "confidence": 0.9,
    },
    # ------------------------------------------------------------- build ---
    "build.webpack_resolve": {
        "severity": "medium",
        "root_cause": "The bundler could not resolve that import path.",
        "fix_steps": [
            "Case matters on Linux even when it works on macOS \u2014 check exact capitalisation",
            "Relative path wrong? ./ and ../ are relative to the importing file, not the project root",
            "Path alias (@/components)? It must be configured in BOTH tsconfig paths and the bundler",
            "Package import? Confirm it is installed and in dependencies, not devDependencies",
        ],
        "verify_command": "ls -la <path>",
        "confidence": 0.85,
    },
    "build.vite_resolve": {
        "severity": "medium",
        "root_cause": "Vite could not resolve the import.",
        "fix_steps": [
            "Vite requires file extensions for CSS and asset imports",
            "Aliases go in vite.config resolve.alias, and must match tsconfig paths",
            "Bare imports must be installed; Vite does not fall back to node_modules guessing",
            "After installing a new dependency, restart the dev server \u2014 pre-bundling is cached",
        ],
        "verify_command": "ls -la <path>",
        "confidence": 0.85,
    },
    "build.ts_2307": {
        "severity": "medium",
        "root_cause": "TypeScript cannot find the module or its type declarations.",
        "fix_steps": [
            "The JS may exist while the types do not: npm i -D @types/<package>",
            "Local file? Check the path and that it is inside tsconfig include",
            "Path alias needs both tsconfig paths AND the bundler's resolver",
            "Some packages ship types only under specific moduleResolution settings \u2014 try 'bundler' or 'node16'",
        ],
        "verify_command": "npx tsc --noEmit",
        "confidence": 0.85,
    },
    "build.gradle_resolve": {
        "severity": "medium",
        "root_cause": "Gradle could not resolve a dependency in the configuration.",
        "fix_steps": [
            "./gradlew <task> --stacktrace   names the specific artifact",
            "Check the repositories block actually includes where the artifact lives",
            "Private repo? Credentials must be in gradle.properties or the environment",
            "Stale cache: ./gradlew --refresh-dependencies",
        ],
        "verify_command": "./gradlew dependencies --configuration runtimeClasspath",
        "confidence": 0.8,
    },
    "build.gradle_failed": {
        "severity": "medium",
        "root_cause": "The build failed. The 'What went wrong' block names the cause.",
        "fix_steps": [
            "Read the '* What went wrong:' section \u2014 it is usually precise",
            "./gradlew <task> --stacktrace --info   for the full trace",
            "Java version mismatches are the most common cause: check JAVA_HOME against the toolchain",
        ],
        "verify_command": "./gradlew <task> --stacktrace",
        "confidence": 0.65,
    },
    "build.maven_goal": {
        "severity": "medium",
        "root_cause": "A Maven plugin goal failed. The real error is above this line.",
        "fix_steps": [
            "Scroll UP \u2014 'Failed to execute goal' is the wrapper, not the cause",
            "Compiler failures list the offending file and line above",
            "mvn -e -X <goal>   for the full stack trace",
            "Check maven.compiler.source/target against the JDK actually in use",
        ],
        "verify_command": "mvn -e clean install",
        "confidence": 0.7,
    },
    "build.make_failed": {
        "severity": "low",
        "root_cause": "A make recipe exited non-zero. The real error is above this line.",
        "fix_steps": [
            "Scroll UP \u2014 the failing command's own output is the diagnosis",
            "Run the failing command directly to see clean output",
            "Recipes must be indented with a TAB, never spaces",
        ],
        "verify_command": None,
        "confidence": 0.5,
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
