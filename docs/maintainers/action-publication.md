# Release the action and publish it in Marketplace

The root [action metadata](../../action.yml) and
[release workflow](../../.github/workflows/release.yml) share the CLI's exact
release tags. The source action defaults its CLI version from its own
`github.action_ref`; it needs a published CLI archive and `checksums.txt` for
that tag. Tag creation and Marketplace publication are manual maintainer
steps. This page describes the planned publication path; it does not claim
that the action is listed or that `v0.2.0` is available.

## Before tagging

Complete the [release checklist](../../CONTRIBUTING.md#releasing), including
the vulnerable-to-fixed inspection, dated CHANGELOG section, NOTICE, and
archive checks. Verify the candidate contains the root action and scripts.
Run the action boundary checks:

```bash
python3 -B scripts/test-actions-gate.py
```

The action uses the supported `shield` icon with a green background.
GitHub's [metadata reference](https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax#branding)
defines those values. Check the repository is public, with one root action
metadata file, and validate `name: weavegate gate` in GitHub's release UI;
name availability cannot be established by the local tests.

## Prerelease adoption check

1. Prepare a dated CHANGELOG section for the exact candidate version, laid
   out under the
   [release-candidate rule](../../CONTRIBUTING.md#release-candidate-changelog-sections), and
   create its `v0.2.0-rc.N` tag manually. Do not select Marketplace publication
   for a prerelease.
2. Wait for the release workflow to publish the Linux archives and
   `checksums.txt` and verify the CHANGELOG-derived release body. A tag alone
   is insufficient: installation fails while assets are absent.
3. From a separate caller checkout, use that exact action tag without
   `version`, with the configuration and scenario in that checkout. Run the
   vulnerable variant, retain its evidence, then replay the same schedule
   against the fixed variant. Check exit 2/failure and exit 0/success,
   respectively, together with the report, schedule, and artifact URL.

This is the release-based adoption check tracked by
[#114](https://github.com/weavegate/weavegate/issues/114). The action's local
Python tests and smoke workflow's explicit `v0.1.0-alpha` checks verify their
own boundaries; they do not replace this check of the published tag default.
Do not describe Spring adoption as complete without its separate evidence.

## Final release and Marketplace publication

After the adoption check passes and its fixes are included, complete the
release checklist for `v0.2.0`, including its consolidated final CHANGELOG
section under the same rule, and create the final tag manually. Wait for the
release workflow to finish before updating its release in the GitHub UI.

Follow GitHub's
[publication instructions](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace):

1. Sign in as the publishing maintainer with two-factor authentication.
   The repository or organization owner accepts the GitHub Marketplace
   Developer Agreement; the release checkbox stays disabled without it.
2. Open the final release's editor and select **Publish this Action to the
   GitHub Marketplace**. Address metadata validation errors before
   proceeding. The name must avoid collisions with existing actions,
   accounts other than the publisher's own account, categories, and reserved
   GitHub features.
3. Choose the primary category, keep the exact final tag, title, and
   CHANGELOG-derived body, and update the release with Marketplace
   publication selected. Do not replace the release notes by hand;
   `CHANGELOG.md` remains their source of truth.
4. Open the listing and run its generated `uses` snippet from a separate
   caller repository. Supply `config` and `scenario`, omit `version`, and
   repeat the vulnerable/fixed evidence check above. Confirm the retained
   `install.txt` names the same final CLI tag as the action reference.
5. Record the listing URL and reproducible workflow evidence in
   [#116](https://github.com/weavegate/weavegate/issues/116). Update the
   user guide's planned fragment only after the referenced release exists.

The release workflow publishes CLI assets and notes; it does not accept the
Marketplace agreement or publish the listing. If metadata or repository
validation blocks listing, record the concrete blocker in #116 and keep
exact-tag action use available while deciding the publication follow-up.
