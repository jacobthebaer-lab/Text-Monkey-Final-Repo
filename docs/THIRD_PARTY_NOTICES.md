# Third-party license inventory

Reviewed October 3, 2026, without installation, dependency changes or deployment.
Text Monkey keeps its existing [MIT license](../LICENSE). Third-party components
retain their own licenses; this identification record is not legal certification
or a complete binary redistribution notice bundle.

## Verified coverage

Canonical source: `c2152aab6ea5556845ca20ad9814d74de2dfc82c`. The isolated cloud
PR4 tree was read at `b5de59a4c6b01fe227f803dc0087588fb3529dac`; no merge or runtime
operation was performed. Exact versions, evidence paths and SHA-256 hashes are
in [the machine-readable inventory](third-party/inventory.json).

| Scope | Coverage | Evidence |
| --- | --- | --- |
| Python | All 13 declared requirements plus their active extras/transitive closure: 71 installed distributions, none missing | [Package list](third-party/python-packages.md); installed METADATA and license text files for every distribution |
| Admin tooling | All 91 non-root lockfile package entries, including platform-optional packages | [Locked package list](third-party/web-packages.md); every entry declares a license; installed texts unavailable in the inspected locations |
| Cloud PR4 Node | All 3 lockfile entries: Playwright 1.56.1, playwright-core 1.56.1, fsevents 2.3.2 | [Locked package list](third-party/cloud-packages.md); matching installed versions and LICENSE files; both Playwright packages also contain NOTICE |
| Brand kit | All 22 tracked brand files: 15 PNGs, 3 fonts, 2 OFL notices and 2 metadata files | File hashes in inventory; existing brand documentation and embedded font copyright/license names |
| Evidence images | All 38 tracked screenshot images | Paths/hashes inventoried; individual contents and permissions not certified |

Python coverage is the actual macOS CPython 3.13 environment, with requirement
markers and requested extras evaluated locally. `requirements.txt` is mostly
version ranges, not a frozen resolution. This inventory does not establish the
different Python 3.12/Linux image resolution or future installs.

## Runtime dependencies and fonts

The Python table identifies every package's declaration and local evidence.
Direct dependencies include FastAPI/SQLAlchemy/APScheduler/OpenAI SDK/Twilio/
Google API client, Uvicorn, Jinja2, python-dotenv, pytest, HTTPX, python-multipart
and psycopg. Inventory does not mean an unused transport is authorized or enabled.
Jinja2's local three-clause text establishes BSD-3-Clause; pyasn1_modules' local
two-clause text establishes BSD-2-Clause where metadata is less specific.

Psycopg and psycopg-binary 3.3.6 declare LGPL-3.0-only. The admin tooling lock also
contains 14 sharp/libvips platform entries whose declarations include LGPL,
alongside MIT and Apache licenses. Preserve those declarations; do not relabel
all dependencies MIT or assume build tools ship in the browser.

Bundled fonts are SIL OFL-1.1, with existing complete notices:

- Bagel Fat One: [Bagel-Fat-One-OFL.txt](../web/texty/public/brand/fonts/Bagel-Fat-One-OFL.txt),
  copyright 2022 The Bagel Fat Project Authors. This covers `bagel-fat-one.ttf`.
- DM Sans: [DM-Sans-OFL.txt](../web/texty/public/brand/fonts/DM-Sans-OFL.txt),
  copyright 2014 The DM Sans Project Authors. This covers the 400 and 600 files.

Their copyright strings match the font name tables. Retain both existing notices
with redistributed fonts. [Brand integration](TEXT_MONKEY_BRAND.md) records the
official Google Fonts sources. No font or artwork was changed during this audit.

## Cloud PR4 infrastructure sources

These sources identify primary tool licenses; exact deployed image contents
and their complete bundled notices have not been inventoried.

| Component in PR4 | Verified primary source | Limit of evidence |
| --- | --- | --- |
| Oracle OCI Terraform provider 9.3.0 | [Release LICENSE.txt: MPL-2.0](https://raw.githubusercontent.com/oracle/terraform-provider-oci/v9.3.0/LICENSE.txt) | Version/checksums are locked; installed provider binary has no neighboring license file in the inspected directory |
| cloudflared 2026.9.3 | [Release LICENSE: Apache-2.0](https://raw.githubusercontent.com/cloudflare/cloudflared/2026.9.3/LICENSE) | Compose pins its digest; complete image/bundled notices not captured |
| actions/checkout, pinned v5 commit | [Pinned LICENSE: MIT](https://raw.githubusercontent.com/actions/checkout/08c6903cd8c0fde910a37f88322edcfb5dd907a8/LICENSE) | CI tooling, not application browser code |
| Node 22 image family | [Official core license and bundled notices](https://raw.githubusercontent.com/nodejs/node/v22.x/LICENSE) | Core MIT plus third-party notices; exact base-image patch/digest unpinned |
| Python 3.12 image family | [Official license and historical notices](https://raw.githubusercontent.com/python/cpython/3.12/LICENSE) | Exact base-image patch/digest unpinned |
| Terraform CLI 1.13.5, used for PR4 validation | [Release LICENSE: BUSL-1.1 with HashiCorp's Additional Use Grant](https://raw.githubusercontent.com/hashicorp/terraform/v1.13.5/LICENSE) | PR4 records official Docker image 1.13.5 `init -backend=false`, `fmt` and OCI 9.3.0 provider-backed `validate`, without network or credentials. No provisioning was performed |
| Terraform CLI, allowed future range 1.5 to below 2 | [1.5.7 MPL-2.0](https://raw.githubusercontent.com/hashicorp/terraform/v1.5.7/LICENSE), [1.6.0 BUSL-1.1](https://raw.githubusercontent.com/hashicorp/terraform/v1.6.0/LICENSE) | Wider range remains unpinned and crosses license regimes; the validated 1.13.5 tool does not establish every future operator environment |

The validated tool/version evidence is recorded in
[PR4's provisioning proof](https://github.com/jacobthebaer-lab/text-monkey/blob/b5de59a4c6b01fe227f803dc0087588fb3529dac/docs/CLOUD_VM_PROVISIONING.md#validation-performed-for-this-build).
This documentation audit verified the 1.13.5 release license without running or
installing Terraform. It does not certify any future production use under BUSL.

## Remaining evidence and redistribution work

- Before distributing installed packages or images, retain their actual LICENSE,
  copyright and applicable NOTICE files, including Playwright's notices. The
  inventory's metadata and links are not substitutes for those distribution files.
- Review LGPL/MPL component source and other distribution obligations for the
  actual artifacts being delivered. No corresponding-source/relinking package or
  binary-distribution compliance proof was prepared in this documentation pass.
- Inventory exact Linux image/apt packages, Chromium and its third-party notices,
  dumb-init, CA certificates, Debian/Ubuntu host packages and Docker components if
  those artifacts are redistributed. PR4 does not lock the complete OS package set.
- psycopg-binary contains libpq, OpenSSL, Kerberos and LDAP native libraries.
  Their individual versions/licenses/notice coverage were not established by the
  wheel's top-level LGPL declaration. Cryptography also bundles native components;
  its package notices alone do not certify every binary's notice coverage.
- The supplied brand kit has usage guidance but no separate explicit artwork
  license or ownership/release evidence in the inspected files. Project MIT and
  font OFL notices do not prove rights to each PNG or included trademark.
- Screenshot content, identifiable people/voices, church/customer logos and any
  later video/audio require their own permission/release review. The 38 screenshot
  records are inventory entries, not a public-demo approval. No tracked demo audio
  or video files were found at this source checkpoint.

Package identification is complete for the bounded scopes above. Keep the
remaining items explicit; do not mark broader license, rights or submission
compliance complete from this inventory.
