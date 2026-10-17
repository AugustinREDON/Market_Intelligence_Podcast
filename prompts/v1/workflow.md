# Market Intelligence Podcast — Automated Daily Workflow

## Global Workflow Instructions

Execute this entire workflow in order during one run.

The three supporting instruction documents are authoritative for their respective stages.

All three are located inside the Google Drive folder path:

`Market Intelligence Podcast/Prompts`

The supporting documents are:

- MIP Briefing Prompt
- MIP Podcast Prompt
- MIP Delivery Instructions

Read each relevant document in full from the Prompts folder before executing its corresponding stage.

The workflow contains three stages:

1. **Stage 1 — Market Intelligence Briefing:** research current markets and create the daily briefing plus carry-forward state using the instructions in MIP Briefing Prompt.
2. **Stage 2 — Podcast Script Generation:** use the Stage 1 briefing as the authoritative source and create the final two-host podcast script using the instructions in MIP Podcast Prompt.
3. **Stage 3 — Google Drive Delivery:** deliver all generated files according to the instructions in MIP Delivery Instructions.

Do not stop after Stage 1 or Stage 2.

When instructions inside a stage say to "return" an output, interpret that as creating the required file for that stage and retaining it for the rest of this workflow.

The Global Workflow Instructions take precedence over any stage-local output wording that would otherwise cause the workflow to terminate early.

## Run Isolation and Context Authority

Treat each scheduled execution as a new, self-contained daily run.

Do not use prior messages, outputs, instructions, generated briefings, generated podcast scripts, or other information from earlier executions of this conversation as source material, continuity context, formatting precedent, or instructions for the current run unless that information is explicitly provided again by the current workflow.

The only permitted continuity input from a prior run is the most recent valid `carry_forward_topics_YYYY-MM-DD.json` file selected during Workflow Initialization.

Current instructions and current-run files always supersede anything that may appear in earlier conversation history.

Do not infer current host names, formatting, editorial preferences, story selection, episode structure, or workflow behavior from previous executions. Use only the current versions of the prompt documents and the inputs explicitly authorized by them.

## Workflow Initialization

Determine the current date and time in U.S. Eastern Time.

Use live, current web research for Stage 1 as required by the MIP Briefing Prompt.

Before beginning Stage 1, use the connected Google Drive to locate the folder path:

`Market Intelligence Podcast/Carry Forward`

Within that folder, look for the most recent prior file matching:

`carry_forward_topics_YYYY-MM-DD.json`

Use the date contained in the filename to identify the most recent valid prior file.

The carry-forward file used for continuity must be dated before the current U.S. Eastern date. Do not use a same-day carry-forward file from an earlier or partial run as the continuity input for the current run.

If a valid prior carry-forward file exists, read it and provide it to Stage 1 as continuity context.

If none exists, continue normally without one.

Do not use a prior daily briefing or podcast script as a substitute for the carry-forward file.

## Stage 1 — Market Intelligence Briefing

Within the Google Drive folder path:

`Market Intelligence Podcast/Prompts`

read the Google Doc named exactly:

`MIP Briefing Prompt`

Read it in full and execute its instructions directly.

Do not analyze, critique, summarize, rewrite, optimize, or suggest improvements to those instructions.

Provide any prior Carry-Forward Topics file found during Workflow Initialization as continuity context.

Create and retain the two Stage 1 outputs required by the briefing instructions:

- `market_intelligence_briefing_YYYY-MM-DD.md`
- `carry_forward_topics_YYYY-MM-DD.json`

The completed Market Intelligence Briefing is the authoritative input for Stage 2.

Do not reproduce either Stage 1 file in the chat.

Continue immediately to Stage 2.

## Stage 2 — Podcast Script Generation

Within the Google Drive folder path:

`Market Intelligence Podcast/Prompts`

read the Google Doc named exactly:

`MIP Podcast Prompt`

Read it in full and execute its instructions directly.

Use the complete `market_intelligence_briefing_YYYY-MM-DD.md` created during Stage 1 as the briefing supplied to the podcast-generation instructions.

Do not analyze, review, critique, summarize, rewrite, optimize, or suggest improvements to the podcast instructions.

Do not perform new market research during Stage 2 except where the MIP Podcast Prompt explicitly permits it.

Create and retain the Stage 2 output:

`MIP_script_YYYY-MM-DD.md`

Do not reproduce the podcast script in the chat.

Continue immediately to Stage 3.

## Stage 3 — Google Drive Delivery

At this point there must be exactly three generated daily output files:

- `market_intelligence_briefing_YYYY-MM-DD.md`
- `carry_forward_topics_YYYY-MM-DD.json`
- `MIP_script_YYYY-MM-DD.md`

Within the Google Drive folder path:

`Market Intelligence Podcast/Prompts`

read the Google Doc named exactly:

`MIP Delivery Instructions`

Read it in full and execute its instructions directly.

Do not analyze, critique, summarize, rewrite, optimize, or suggest improvements to those instructions.

Complete all required file creation, verification, upload or update, and Google Drive delivery steps before considering the workflow complete.

If all three files are successfully delivered, follow the completion-response instructions in MIP Delivery Instructions.

If any delivery fails, follow the failure-handling instructions in MIP Delivery Instructions.

Do not disable the recurring scheduled task because one run fails.

Report the failure and leave the schedule enabled unless the user explicitly asks to pause or disable it.