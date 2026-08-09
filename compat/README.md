# compat/

Build inputs that exist only to bridge a version mismatch between two
pinned upstreams. Nothing here is MCUHome API, nothing here is compiled
into an image on its own, and every entry states the condition under
which it can be deleted.

## `mbedtls/` — legacy-header shims for mbedTLS 4

Nine one-line forwarding headers: `bignum.h`, `ccm.h`, `ctr_drbg.h`,
`ecdsa.h`, `ecp.h`, `entropy.h`, `pkcs5.h`, `sha1.h`, `sha256.h`. Each
one is a single `#include <mbedtls/private/…>` of the same file name.

**Why.** Zephyr v4.4.0 ships mbedTLS 4, which split the library: the PSA
API stays in `modules/crypto/mbedtls/include/mbedtls/`, while the legacy
classic-API headers moved into tf-psa-crypto's builtin driver, under
`mbedtls/private/`. connectedhomeip v1.5.1.0 predates that move and
still includes the old spellings — `src/crypto/CHIPCryptoPALPSA.cpp:38`
(`<mbedtls/bignum.h>`) and `:39`, `src/crypto/CHIPCryptoPALmbedTLS.h:22`
and `src/crypto/CHIPCryptoPALmbedTLSCert.cpp:31` (`<mbedtls/ecp.h>`) are
the ones the MCUHome configuration actually compiles, because
`chip_crypto = "psa"` selects the `cryptopal_psa` source set
(`src/crypto/BUILD.gn:161-176`). Neither path resolves against mbedTLS 4
any more, so the CHIP library does not build without these.

The full nine are the set the classic mbedTLS PAL (`chip_crypto =
"mbedtls"`) reaches; they are kept together so a change of crypto
back end is a one-line GN argument and not a hunt for the next missing
header.

**How they are reached.** `patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch`
adds `-I${ZEPHYR_MCUHOME_MODULE_DIR}/compat` to CHIP's compile flags, so
this directory — not `compat/mbedtls/` — is the include root, and the
`mbedtls/` sub-directory is the namespace CHIP asks for. That is also
why this README lives one level up: everything inside `compat/mbedtls/`
is addressable as `<mbedtls/…>` by any translation unit CHIP compiles.

The shims come last in that flag block, after the real mbedTLS and
tf-psa-crypto include directories, so they can only ever satisfy an
include that upstream leaves unresolved.

**When to delete this.** As soon as the CHIP pin moves to a release that
includes the mbedTLS 4 header layout (`<mbedtls/private/…>`, or the PSA
API alone), or the mbedTLS pin goes back to a 3.x line that still ships
the classic headers in their old place. The check is mechanical: drop
the `-I` line from the chip-module hunk of the patch and rebuild the
Matter library — if it compiles, this directory is dead weight. Delete
the directory and the flag together; a stale include path that resolves
to nothing is how a build starts depending on include order.

**Licensing.** These are forwarding headers written for MCUHome. No
upstream mbedTLS or CHIP code is reproduced here, so they carry the
repository's own Apache-2.0 headers like every other file.
