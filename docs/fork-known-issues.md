# Fork: known issues to address

Notes for this fork's maintainers — found while porting onto upstream 0.9.68.

## Terraform ids in the AST cache are tied to the absolute directory

**Where:** upstream `graphify/extractors/terraform.py` (`_scope_id`, `prepare_terraform`)
together with the AST cache in `graphify/cache.py`.

**What:** `extract_terraform` scopes node ids by
`_scope_id(<absolute parent dir>)`, which embeds both a slug of the absolute path
and a sha256 digest of it (e.g. `terraform_c_users_..._tf_<digest>_aws_instance_web`).
The per-file AST cache is keyed by file content + path relative to the scan root,
and its id-portability pass (`_relativize_ids_in`) only strips ids that *start
with* the root slug — Terraform ids start with `terraform_`, so they are stored
with the absolute form. `prepare_terraform` then only rewrites ids whose prefix
matches the *current* absolute directory. A cache entry written under a different
absolute location therefore replays stale, non-portable ids.

**When it bites:** the same `graphify-out/` cache is reused from a different
absolute location — the repo moved or renamed, a copied `graphify-out/`, a
shared absolute `GRAPHIFY_OUT`, or `extract()` called from the API without
`cache_root` (it defaults to CWD, so two scan roots share one cache). Same-named
directories inside one project (`dev/app` vs `prod/app`) are **not** affected:
the relative path is part of the cache key.

**Repro:** extract `tf/main.tf` (`resource "aws_instance" "web" {}`) under root A
with `cache_root=X`, then the identical file under root B with the same
`cache_root=X`; the `aws_instance.web` node id still contains root A's path.
(`tests/test_iac_pipeline.py` avoids it by passing a per-test `cache_root`.)

**Fix direction:** make the cached result directory-agnostic — e.g. mint
Terraform ids from a placeholder scope in the extractor and substitute the
root-relative scope in `prepare_terraform` (which already runs on cache hits),
or teach `_relativize_ids_in`/`_absolutize_ids_in` the `terraform_<dir>_<digest>`
form. Worth offering upstream as a PR since it is not fork-specific.
