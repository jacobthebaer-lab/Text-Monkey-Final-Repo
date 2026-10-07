# Text Monkey recording and submission package

**Historical October 4 recording package.** For the current October 6 build
document, integrated ranking, test results and remaining submission work, use
[current readiness](READINESS.md) and the [Agent Build Document](../AGENT_BUILD.md).
The preview recording below is historical. `PROJECT_DESCRIPTION.txt` now contains
the current edited 250-word description and is separate from that old recording.

Prepared October 4, 2026 from canonical `7691251`. Draft for independent review and human submission, not a submitted entry or compliance certification.

## Ready assets

- [English project description](PROJECT_DESCRIPTION.txt): exactly 250 whitespace-delimited words, with no title included in the count. Paste only the file's text into the description field and verify the organizer's count.
- [Timed summary and shot manifest](storyboard.json): 84 seconds, ten actual UI states, English captions, no narration or music. The captions are also a ready-to-read summary script. Rehearse the delivery within 90 seconds if a person presents it live.
- Native macOS compositor: [render_submission_walkthrough.swift](../../tools/render_submission_walkthrough.swift). It reads supported-browser PNG captures, adds a persistent fictional-preview label and captions, writes H.264 MP4, and verifies the entire decode, dimensions and duration. Captures and media belong outside Git.

The recording is a walkthrough assembled from fresh captures of the actual fictional public UI. It shows opening the preview, navigating the roster and profile, reviewing coverage, applying one manual sample cancellation, inspecting its changed state, and opening Messages and Settings. It contains no invented app screens, real roster, human face, voice, credentials or live conversation. It is not a continuous screen recording and does not establish connected backend, live Gloo, ranking or native delivery behavior. The separate coherent backend rehearsal supplies its own evidence; do not combine proof claims or identities across the two.

## Timed summary and shot list

| Time | Actual browser capture | Shot title | Caption / summary script |
| --- | --- | --- | --- |
| 00–06s | 01-entry.png | Ordinary texts. A clearer week. | Text Monkey brings volunteer scheduling and coordinator decisions together. This walkthrough uses fictional data in the disconnected public preview. |
| 06–18s | 02-home.png | See what needs you | Open the demo. Home shows open spots, decisions waiting, and upcoming event coverage. The coordinator can see where attention is needed. |
| 18–28s | 03-volunteers.png | Keep the important facts visible | Open Volunteers. Availability, recorded consent, and clearance are shown together. These sample records do not establish consent for real people. |
| 28–36s | 04-profile.png | Review a fictional profile | Open a sample profile, then cancel without saving. A phone number does not grant an administrator login. |
| 36–46s | 05-shifts.png | Find the staffing gap | Open Shifts. Six of eight roles are covered, with two open spots. The preview keeps its scheduling and delivery limits visible. |
| 46–54s | 06-sample-action.png | Try the manual sample controls | The preview has manual booking controls. Only the selected slot is affected. Automatic replacement search, live AI, and texts do not run here. |
| 54–62s | 07-sample-result.png | The selected slot reopens | Cancel the fictional nursery booking. Coverage changes from two people to one. The visible notice confirms that no replacement was assigned. |
| 62–72s | 08-messages.png | Make the decision understandable | Open Messages. A fictional cancellation and exact sample response are visible for review. Approval is disabled here. Connected interpretation and composition use Gloo. |
| 72–80s | 09-settings.png | Know whether the connection is ready | Open Settings. Connected admins save their mobile number for updates. This preview cannot save a real recipient or deliver a message. |
| 80–84s | 10-settings-limits.png | Clear limits. Useful next steps. | Preview only. Replacement ranking is unfinished; live integration writes stay held. |

The fictional seed is Cedar Hills Community Church. Home initially shows two open spots. The schedule has six of eight assignments covered. A manual cancellation of Jen's nursery sample booking reopens only that slot: nursery coverage changes from two of two to one of two, overall coverage to five of eight, and open roles to three. The actual result notice says no replacement was assigned. The existing fictional cancellation in Messages is a separate seed review example, not evidence that the manual sample action called Gloo or created a backend proposal. Approval is disabled. Settings identifies Gloo, delivery, scheduling and Planning Center review as disconnected or preview-only.

Do not present the manual cancellation as Clyde's replacement algorithm. Do not claim live two-way Planning Center writes, production texting readiness, or cloud/native delivery from these captures. The connected application uses Gloo for interpretation and composition and holds when unavailable; the public preview calls no live model.

## Capture and render handoff

Use a separate temporary fictional-demo tab at [the public preview](https://text-monkey-demo.pages.dev/). Keep the signed-in admin runtime and its private browser session untouched. Capture the actual current UI through supported browser control. Store the ten named PNGs in a private `captures` directory. Only fictional local preview actions are part of this shot list. Close the temporary tab after capture; do not modify backend settings or enable a transport.

Run on macOS with the installed Swift/Xcode command-line toolchain:

```sh
swift -suppress-warnings tools/render_submission_walkthrough.swift \
  docs/submission/storyboard.json /absolute/private/captures \
  /absolute/private/TextMonkey-walkthrough.mp4
```

Choose a fresh output path: the script refuses to overwrite an existing file. It produces `video-proof.json` and decoded `playback-01.png` through `playback-10.png` alongside the MP4. Inspect those frames for readable, upright captions and accurate screenshot provenance. Use the verified final file, not an interrupted render. Keep the media private until the final rights and entry checks are complete.

## Final human submission checklist

Current official requirements were verified by the project coordinator from the September 29 Official Rules tab on October 4. The [official rulebook](https://docs.google.com/document/d/1jmH6lnQkI_B9YQeHT12pCanQI8W4yoE_yK3EO2IWaeQ/edit) governs. These are submission checks, not new texting or software requirements.

- [ ] Confirm each actual team member is at least 18 and has reached the age of majority in their jurisdiction, individually applied, received approval, and accepted the rules. Confirm team authority and one entry only.
- [ ] Identify at least one team member attending the required Boulder event and final presentation on October 8. Do not infer attendance or acceptance from repository access.
- [ ] Confirm the organizer's current entry form, code-access instructions and designated Drive destination. Keep the repository private; grant only the approved access required for judges. Do not upload to a guessed destination.
- [ ] Review the 250-word English description, summary script and final exported MP4. Verify the exact upload file is no longer than 90 seconds and plays correctly. Every entrant presents a 90-second summary. The working-application demo video is optional at preliminary submission and required for finalists.
- [ ] Complete preliminary submission by October 7, 2026, 9 PM Mountain. Complete finalist submission by October 8, 2026, 9 AM Mountain when applicable. Confirm any organizer update before the final action.
- [ ] Preserve competition-period history beginning September 8, 2026. Identify eligible work and progress; do not rewrite history to imply eligibility. Submit code only through the organizer-defined GitHub or Drive process.
- [ ] Retain the existing [MIT LICENSE](../../LICENSE), [third-party inventory and notices](../THIRD_PARTY_NOTICES.md), and bundled font OFL notices. Check actual distribution obligations for any binaries or fonts being delivered.
- [ ] Verify rights to the supplied artwork, logo, trademarks and other included assets. Existing MIT and font notices do not independently establish artwork ownership. Fictional sample names are labels, not identifiable people shown on camera.
- [ ] Obtain written releases for every identifiable person or voice if later footage or narration is added. This silent draft includes no human likeness or voice and does not claim releases exist.
- [ ] Review all public material for private contacts, conversations, credentials and receipts. Keep this fictional capture set separate from connected runtime proof.
- [ ] Review every completion claim against current evidence. Replacement ranking remains unfinished and live two-way integration writes remain held. Update only after independently verified completion; no future feature should be presented as working.
- [ ] Have the authorized human perform the actual submission/upload, retain its receipt, and verify the intended destination and final entry. Do not mark this complete from a prepared file.

Judging assigns equal 20% weight to Concept/Product, Innovation, Impact/Execution, AI, and Presentation. The package explains the coordination problem and workflow, shows a real fictional UI state change, identifies Gloo's connected role, and makes evidence limits visible. The AI and backend claims need the separately verified rehearsal artifacts; the UI video cannot substitute for them.
