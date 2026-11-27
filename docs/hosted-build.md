# Hosted WebView build plan

Build on GitHub-hosted Linux. The local and hosted paths should use the same stage commands. Keep release publication gated on verification of the candidate APK and its license material.

## Run a candidate build

Select **Build WebView** in GitHub Actions and choose the branch to build. Provide `inputs-url` and `inputs-sha256` for a prepared-input manifest. The workflow uses the staged Chromium dependencies described in [Prepare inputs](build.md#prepare-inputs). Automated cold intake is pending.

The manifest is a JSON object with the keys `chromium`, `clang`, `gn`, `sdk`, and `jdk`. Each value is an ordered array of objects with an HTTPS `url` and a lowercase `sha256`. The workflow extracts the archives for each component into its own directory. Use rootless tar archives, with `DEPS` at the Chromium root, `gn` at the GN root, and `bin/java` at the JDK root. Supply the Chromium tree before the repository patch series and omit `third_party/jdk/current` so the workflow can link the staged JDK there. Audit the archives before making a manifest available to runners.

Find the candidate APK and its license material in the branch's draft Release under the `apk-` asset name printed by the job. APK verification runs after the upload and also runs when an APK is reused.

## Stage boundaries

The local Chromium 156.0.8078.93 build for Android API 37.0 x86_64 recorded 45,723 Ninja actions for `apks/SystemWebView64.apk`. Action times below are summed across parallel work and describe the local build. GitHub job durations remain to be measured.

| Stage | Prerequisites | Actions | Summed local action time | Outputs |
| --- | --- | ---: | ---: | --- |
| Prepare | Pinned sources, toolchain and GN configuration | 11,524 | 7.515 h | Build graph, generated sources and headers, host tools, and shared native prerequisites |
| Native compile matrix | Prepare | 29,791 | 45.042 h | 24 batches of Android object files |
| Java and resources | Prepare | 2,437 | 0.190 h | R8 DEX and optimized resources |
| Native link | All native batches and prepare | 1,970 | 0.145 h | `libwebviewchromium.so` and its link inputs |
| Package APK | Native link and Java and resources | 1 | 0.001 h | `SystemWebView64.apk` |

Preparation brings forward 1,556 Android objects needed by resource compilation and DEX generation, as well as the remaining actions shared by Java and native linking. For example, the resource target depends through GN's order-only edges on the Crashpad handler trampoline, which reaches Perfetto objects. With that closure in preparation, no declared dependency runs between native batches or from the Java job to native linking. Preserve the 48 additional checks and wrapper actions reached by the public `system_webview_apk` target when validating the completed APK.

## Job sizes and feedback

The planning job emits both compile matrices from the Ninja stage plan. The workflow expands their stage lists with `fromJSON`. Preparation and linking use those same lists to locate their prerequisite assets, including when the plan comes from the branch's draft Release.

Balance native batches by recorded Ninja action times. Blink contributes 9,125 objects and about 45% of native batch time, so split its objects across batches. The layouts below cover the same objects with no declared dependencies between batches.

| Native jobs | Summed action time per job | Raw primary objects per job |
| ---: | ---: | ---: |
| 8 | 5.63 h | 398–631 MiB |
| 16 | 2.82 h | 180–342 MiB |
| 24 | 1.88 h | 118–233 MiB |
| 32 | 1.41 h | 74–187 MiB |

The workflow uses 24 batches and balances targets by source directory using a profile from the pilot. Against the pilot's per-action timings, these batches range from 1.79 to 1.92 summed action-hours. That gives about 27–29 minutes of idealized compile work with four workers at the recorded rates. GitHub Free permits 20 concurrent standard jobs, so account for queueing when measuring a full run. Evaluate 32 batches if individual jobs remain too slow and repeated setup and transfer costs are small. [Runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories) · [Concurrency limits](https://docs.github.com/en/actions/reference/limits#job-concurrency-limits-for-github-hosted-runners)

Split preparation as well. The first job builds the prerequisites of the preparation object files. Four jobs then compile 4,905 independent host and Android objects. The final preparation job completes the remaining archives, tools, and generated outputs. The stages account for 0.87 summed action-hours in the first job, 1.46–1.57 per compile job, and 0.60 in the final job. The static split covers the original 11,524 preparation actions.

Run lint first. Start a compile smoke job after source staging and GN generation, in parallel with full preparation. The selected zlib, base, and Blink Mojo objects exercise both direct compilation and generated C++ inputs. Their combined cold Ninja closure is 4,478 actions and 1.45 summed action-hours, or about 22 idealized minutes with four workers at the recorded rates. Measure checkout, download, GN and compile separately. Use the smoke job for fast feedback on build input changes, and keep the full compile matrix for APK builds.

The final link invocation took 53.5 seconds locally. Its full input inventory spans 34,267 files and 4.14 GiB before toolchain, resources and debug sidecars.

## Reuse between commits

Store reusable build output in one draft GitHub Release per branch. Use the full branch ref to derive a collision-resistant release `tag_name` such as `ci-cache-dev-<ref-hash>`. Name each asset by job and input fingerprint, for example `native-07-<input-hash>.tar.zst`. The same branch can reuse matching assets across commits and workflow runs. Keep this release in draft and publish verified APKs through the regular release workflow. Only trusted same-repository build and cleanup jobs get release-write permission. [GitHub Releases API](https://docs.github.com/en/rest/releases/releases)

Compute each fingerprint from the job's effective inputs, including source and toolchain locks, applicable patches and build scripts, effective GN arguments, ABI, and exact Ninja targets where relevant. Include the release asset IDs and digests of direct upstream handoffs. Record the source commit and run URL as provenance. A packaging-only fix can then reuse native and Java jobs, and a toolchain change invalidates jobs that use it.

Look up a complete matching asset before expensive downloads or compilation. On a hit, return its metadata to downstream jobs and download its payload where needed. On a miss, build and upload the output. Verify the GitHub asset digest before extraction and run the job's checks and final APK validation on the current candidate. Treat missing or incomplete assets as misses. Each file must be smaller than 2 GiB. Split larger handoffs using upload-specific part names and upload the manifest last, recording the ID and digest of each part. [Release asset API](https://docs.github.com/en/rest/releases/assets) · [Release size limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases#storage-and-bandwidth-quotas)

To force a rebuild, delete that job's asset in the draft Release, or delete the manifest for a multipart asset. The next run rebuilds the job. Its new asset ID changes the fingerprint of dependent jobs, so they rebuild even if the regenerated bytes match. Independent jobs can still reuse their assets. Keep successful assets when a sibling job fails.

Put a cleanup workflow on the default branch. On GitHub's `delete` event, use `github.event.ref` and handle branch deletions only. Serialize cleanup with writes to that branch's draft Release. Cache writers check that the branch still exists. Cleanup rechecks that the exact branch ref is absent, then deletes only the matching draft in the CI cache namespace by release ID. A recreated branch or an already absent draft requires no action. [GitHub Actions delete event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#delete) · [Delete a release](https://docs.github.com/en/rest/releases/releases#delete-a-release)

## Hosted runner qualification

The stage runner restores outputs at their original relative paths. It treats restored files as inputs to the stage's Ninja graph, checks that the remaining actions match the plan, and lets Ninja reconstruct the thin archives and generate the link response file.

The pilot transferred a 15-object zlib fixture and produced a byte-identical archive in the receiving workspace. The full build still needs a clean cross-runner handoff test that restores generated headers and host tools, accounts for compiler dependency files, and confirms that archive reconstruction and linking reuse the transferred objects. The preparation graph names about 2.69 GiB of existing outputs. Measure the actual selected transfer set, peak disk use, time spent on transfers, and linker memory in hosted CI.

Qualify reuse with a packaging-only fix in a new commit on the same test branch. Check that a deleted native batch asset causes that batch and its dependent jobs to rebuild, and that deleting the branch removes only its draft Release.

One standard Ubuntu 26.04 runner had 86.69 GiB free after checkout. Its workspace and temporary directories share one filesystem. The local prepared source tree occupied 18 GiB. Use the hosted peak measurements to decide whether source subsets or larger runners are needed. Use 90 minutes as the cold per-job qualification target, including downloads and uploads, to leave margin within the six-hour platform limit. [Runner measurement](https://github.com/aosstudio/webview/actions/runs/46031914677) · [Actions limits](https://docs.github.com/en/actions/reference/limits)
