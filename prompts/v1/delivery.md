# Google Drive Delivery

Execute these instructions after the Market Intelligence Briefing and Podcast Script stages have been completed.

There must be exactly three generated daily output files:

- `market_intelligence_briefing_YYYY-MM-DD.md`
- `carry_forward_topics_YYYY-MM-DD.json`
- `MIP_script_YYYY-MM-DD.md`

All three files must be delivered inside the connected Google Drive folder named exactly:

`Market Intelligence Podcast`

Use the following destination folders:

| File | Destination folder |
| --- | --- |
| `market_intelligence_briefing_YYYY-MM-DD.md` | `Market Intelligence Podcast/Briefings` |
| `carry_forward_topics_YYYY-MM-DD.json` | `Market Intelligence Podcast/Carry Forward` |
| `MIP_script_YYYY-MM-DD.md` | `Market Intelligence Podcast/Scripts` |

## File Handling Requirements

Before calling any Google Drive upload or update action, write each completed output to an actual runtime file using these exact absolute paths:

- `/mnt/data/market_intelligence_briefing_YYYY-MM-DD.md`
- `/mnt/data/carry_forward_topics_YYYY-MM-DD.json`
- `/mnt/data/MIP_script_YYYY-MM-DD.md`

Replace `YYYY-MM-DD` with the current U.S. Eastern date.

The contents must be the complete final outputs produced by the briefing and podcast stages, not placeholders, chat-only text, or code-fenced copies.

Verify that all three runtime files:

- exist;
- are non-empty;
- have the required extensions; and
- contain the expected complete content.

Treat the runtime-produced file reference for each verified file as the authoritative upload input.

## Destination and Existing-File Handling

For each generated file, use only its designated Google Drive destination folder.

### Market Intelligence Briefing

Destination:

`Market Intelligence Podcast/Briefings`

Search only inside this folder for an exact same-name file:

`market_intelligence_briefing_YYYY-MM-DD.md`

If an exact same-name file exists, update that Drive file in place using the verified runtime file as the replacement input.

If no exact same-name file exists, upload the verified runtime file as a new raw file to this folder.

### Carry-Forward Topics

Destination:

`Market Intelligence Podcast/Carry Forward`

Search only inside this folder for an exact same-name file:

`carry_forward_topics_YYYY-MM-DD.json`

If an exact same-name file exists, update that Drive file in place using the verified runtime file as the replacement input.

If no exact same-name file exists, upload the verified runtime file as a new raw file to this folder.

### Podcast Script

Destination:

`Market Intelligence Podcast/Scripts`

Search only inside this folder for an exact same-name file:

`MIP_script_YYYY-MM-DD.md`

If an exact same-name file exists, update that Drive file in place using the verified runtime file as the replacement input.

If no exact same-name file exists, upload the verified runtime file as a new raw file to this folder.

## Google Drive Upload Requirements

When calling a Google Drive upload or update action, pass the actual runtime file reference or absolute runtime path in the action's file input (`file_uri`).

Do not pass merely:

- the filename;
- generated text;
- a chat attachment link;
- a `sandbox:` link;
- a Google Drive URL; or
- an invented file-reference object.

Perform and verify the three Google Drive actions one file at a time.

A successful file-generation step does not count as a successful Google Drive upload.

Do not consider delivery complete until all three destination actions have been successfully completed.

## File Format Requirements

Upload the files as their raw file types.

Preserve the `.md` and `.json` extensions exactly.

Do not convert the Markdown files into native Google Docs.

Do not convert the JSON file into another format.

The podcast script must be an actual `.md` file, not merely Markdown text displayed in the chat.

If a file with the same exact name already exists for the current date in its designated destination folder, replace or update that file rather than intentionally creating a duplicate same-day version.

Do not upload scratch work, research notes, temporary files, prompt text, or other intermediate files.

Only upload the three daily output files listed above.

Do not upload any of the three daily output files directly into the root Market Intelligence Podcast folder.

Do not upload these files to the Episodes, Prompts, or Archive folders.

Do not claim an upload succeeded unless the Google Drive action actually succeeded.

## Completion Response

If all three uploads succeed, return only a concise confirmation listing the three filenames and their destination folders.

Do not reproduce the briefing, carry-forward JSON, or podcast script contents in the chat.

If any Google Drive upload fails, state clearly:

- which file failed;
- which destination folder was being used; and
- whether the other files uploaded successfully.

Provide the generated file as a chat attachment if possible so the failure can be recovered manually.

Do not disable or modify the recurring scheduled task because of an individual delivery failure.