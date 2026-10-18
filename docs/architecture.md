# Architecture and maintenance boundaries

## Separate state, adaptation and hardware evidence

`state/upstream.json` pins one official release, resolved commit, asset ID, URL,
size and SHA-256. The release body is not copied into commands or treated as
instructions. A new state is a request to evaluate an upstream version, not
approval to apply an old binary patch.

`profiles/<tag>.json` describes reviewed transformations of exact Mach-O files.
The adapter checks whole-file hashes and original instructions before writing a
new output. It never edits the official archive or the live driver. A changed
hash, unexpected instruction or missing profile stops the build.

`build-report.json` records the input, profile and output hashes. A successful
hosted build means only that the candidate passed its stated static checks.
`hardware_validation` remains `not-run`; adding an upstream version does not
silently reuse a previous hardware result.

Hardware evidence must bind the candidate archive SHA-256, the actually loaded
encoder library SHA-256 and the upstream tag. It also needs the exact encoder
ID, hardware flag, PCI target, decoded content, forced IDR and timing results.
JSON is an evidence record, not cryptographic proof that a GPU test happened.

## Initial H.264 scope

The official v2.3.6 libraries differ from the previously used 1.9.0 libraries.
The initial candidate therefore uses the new upstream binaries with an explicit
classic-layout adaptation, rather than mixing old codec libraries with a new
kernel driver. HEVC registration and its layout/bitstream issues are left alone.

The H.264 registration bypass is scoped by the application tool to a machine
with exactly one NVIDIA display GPU, PCI `10de:2504`. The patched branch itself
is not a runtime per-device PCI guard. This is an experimental implementation
boundary and must remain visible in documentation and manifests.

The new codec layout adaptation expects ordinary NV12 input. GlideBeam's old
prepacking is restricted to its recognized 1.9.0 library hash, so it must not be
blindly broadened to the new patched library. Otherwise input could be corrected
twice. Validate the driver with the standalone probe before testing remote desktop.

## Build and application

The builder reads only regular files within the official NVMTLDriver bundle,
rejects duplicate and unsafe archive paths, then invokes the adapter. The Intel
macOS job signs the changed H.264 dylib and the enclosing bundle and verifies the
full bundle. The overlay contains only the changed two executables and the new
CodeResources, with source/license and application metadata.

Application is a separate, explicit operation. The read-only check requires the
matching official driver already loaded and every base bundle file unchanged.
The application tool checks overlay hashes and the PCI device, creates a fresh
backup and stages a full copy. Darwin `RENAME_SWAP` changes the directory
atomically. A failed post-check swaps the original back. Neither build nor
application restarts a service, rebuilds a kernel collection, changes OpenCore,
or reboots the machine.

The overlay backup restores the official user-mode bundle of the same upstream
version. It cannot restore an earlier full driver package, EFI or VM disk. Keep
those separately before testing any new official driver version.

## Automated tracking

The scheduled workflow creates or updates one candidate branch/PR in this
repository. It does not submit AI-generated contributions to upstream. It
explicitly dispatches a candidate build so it does not rely on token-generated
PR events running automatically. Duplicate state does not create repeated PRs
or overwrite adaptation work added by a reviewer.

Build workflows have read permissions. Only the upstream tracking job receives
the narrow write permissions needed for its branch, PR and workflow dispatch.
No self-hosted runner executes arbitrary pull-request code automatically.

## Maintenance checklist

For each new upstream release, verify its official asset and resolved source
commit, compare the registration and codec paths, review exact instruction
changes, and extend CPU differential tests. Then build and sign the candidate.
On the target machine, use a recoverable backup and independent hardware-only
probe. Record image correctness, sustained nominal 60 FPS and latency separately
from compilation. Never report software fallback or API initialization alone as
a successful hardware qualification.
