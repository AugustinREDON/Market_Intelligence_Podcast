# Market Intelligence Podcast — Cloud Delivery Specification

## Purpose

Define how application code validates and stores generated text, hands it to the audio workflow, and publishes completed episodes.

Application code performs all storage operations through AWS APIs. AI responses provide content only; they do not prove that files exist or uploads succeeded.

## Configuration

Application code receives the private S3 bucket name through deployment configuration.

Use the validated run context and episode manifest to determine object keys. Do not accept arbitrary storage destinations from model output.

Keep credentials out of prompts, manifests, generated files, and logs.
Use scoped IAM roles for AWS access.

## Text Outputs

Each completed text run produces three content files:

| Content | S3 object key | Content type |
| --- | --- | --- |
| Complete briefing | runs/{run_id}/briefing.md | text/markdown; charset=utf-8 |
| Carry-forward state | runs/{run_id}/carry_forward.json | application/json |
| Podcast script | runs/{run_id}/script.md | text/markdown; charset=utf-8 |

Serialize the carry-forward object as JSON.
Store the briefing and script as UTF-8 text without outer code fences.

## Validation Before Upload

Validate that:

- The run context satisfies its supported schema.
- The intended object keys match the run context.
- Required outputs are present and non-empty.
- The briefing identifies the supplied edition date and research cutoff.
- Carry-forward state is valid JSON with the expected fields and values.
- Carry-forward dates match the run context.
- Every non-empty script line uses HOST_A: or HOST_B:.
- Both hosts have non-empty dialogue.
- The script opening matches the edition date and host identities.
- The required opening and sign-off are present.
- The script satisfies the configured length policy.

A script outside the normal word-count range must be flagged.
It must not silently proceed to audio generation; application code must apply an explicit acceptance or regeneration policy.

Structural validation does not establish factual accuracy.
Research and editorial checks remain required.

## Upload and Verification

Upload validated content to its exact run-specific keys.

Verify successful storage using object size and a content checksum.
Do not assume an S3 ETag is always a content checksum.

Do not mark text delivery complete until all three objects have been uploaded and verified.

Keep generated text private. It is not website content by default.

## Manifest and Audio Handoff

After all three text objects have been verified:

1. Build the episode manifest using application-controlled values.
2. Validate it with the manifest validator.
3. Save and verify it at runs/{run_id}/manifest.json.
4. Preserve the accepted carry-forward state in durable state storage.
5. Mark the text stage complete.
6. Submit the bucket and exact manifest key to the audio workflow.

Use the manifest's exact script and voice keys for audio generation.
Do not search for whichever file happens to be latest.

The manifest is an audio-job input, not evidence of publication.

## Retries and Duplicate Prevention

Reuse the same run_id and selected inputs when retrying a run.

Reuse verified checkpoints rather than regenerating completed content or repeating completed audio work.

Do not overwrite accepted run inputs with different content under the same run_id. A deliberate regeneration uses a new run_id.

Application code must coordinate concurrent workers and publication attempts. Predictable object names alone do not prevent duplicate jobs.

If an episode is already successfully published, a retry must recognize that completion and avoid publishing it again.

Replacing a published edition requires an explicit replacement process.

## Durable Carry-Forward State

Preserve accepted carry-forward state outside expiring run folders.

Record its edition date and originating run_id.

Select only accepted state dated strictly before the next episode_date.
Weekends, holidays, and missed runs may create gaps.

Do not replace newer accepted state with an older run's state.
Coordinate concurrent updates so the accepted state remains consistent.

Audio failure does not invalidate successfully completed text outputs.

## Audio Upload and Publication

The audio workflow must:

1. Retrieve and validate the manifest and required assets.
2. Generate the audio using the specified voices.
3. Assemble and normalize the audio.
4. Produce the 192 kbps MP3.
5. Verify that the MP3 is non-empty, decodable, and has a valid duration.
6. Upload it to the manifest's audio_key.
7. Verify the uploaded audio's size and checksum.
8. Publish metadata to the manifest's metadata_key only after audio verification succeeds.

Episode metadata must identify:

- edition date;
- originating run_id;
- publication timestamp;
- availability expiry timestamp;
- audio object key;
- audio duration in seconds;
- audio size in bytes;
- content checksum.

Only successfully published metadata may make an episode visible on the website.

Failed or partial audio uploads must not create a visible episode.

## Retention and Public Access

Keep the S3 bucket private.
Expose only intended website assets through CloudFront.

Separate permanent assets and durable state from expiring outputs:

- assets/prompts/: versioned instructions.
- assets/voices/: versioned voice references.
- Model assets: separately managed permanent storage.
- Durable carry-forward state: separately managed state storage.
- runs/: generated text and run-specific files.
- published/: finished audio and episode metadata.

Application and delivery logic must enforce the configured 14-day availability window for listings, playback, and direct downloads.

Do not rely solely on S3 lifecycle deletion, which is asynchronous.
Ensure cached delivery cannot extend access beyond the availability window.

Retention rules must not delete prompts, voices, model assets, or required carry-forward state.

## Failure Reporting

Record:

- run_id;
- failed stage or operation;
- affected object key when applicable;
- actionable error details;
- which checkpoints completed successfully.

Do not log credentials or secret values.

Do not claim an upload or publication succeeded without verification.
Leave failed runs recoverable from validated checkpoints.

An individual failure must not disable the weekday schedule.

Compute shutdown and maximum-runtime safeguards must operate even when generation, upload, or publication fails.