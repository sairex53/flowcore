# Fedora SELinux validation

The target is a disposable Fedora system with the targeted policy in
enforcing mode. Capture current enforcement, installed file and socket
labels, process contexts, capabilities, unit properties, audit denials
and resource cleanup. A container domain is a separate observation
from a normally booted guest and its native service labels.

The deployment uses the distribution's existing labels. A new
policy module requires a reproduced denial and an independently
reviewed, narrow rule. Test failures must be retained as evidence.
The runtime never changes SELinux enforcement or installs permissive
policy. [systemd service sandboxing](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)

[Installed runtime](INSTALLED_RUNTIME.md) ·
[Helper security](PRIVILEGED_HELPER_SECURITY.md)
