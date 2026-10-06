# Optional ministry recipe examples

`data/optional_ministry_recipes.json` provides fictional role and event-type examples for the four broader ministry areas in the feature universe. The existing generic recipe mapper remains responsible for matching configured event types and creating their role slots. These examples add configuration coverage, not another scheduling engine.

The catalog is deliberately separate from `data/event_types.json` and `data/roles.json`. The ordinary synthetic seed and live integrations do not read it. No church, event, volunteer, credential, consent or qualification is created by adding this file. There is no automatic installer or activation switch in this patch.

## Coverage

| Universe feature | Existing examples | Optional additions |
| --- | --- | --- |
| `sunday-ministry` | Sunday hospitality, nursery, kids, sound, first aid | Worship team and sound |
| `midweek-ministry` | Kids night | Youth night, adult group hosts, a separate group-with-childcare variant |
| `community-ministry` | Food drive and licensed drivers | Food pantry, community meals, shelter partnership |
| `seasonal-events` | Christmas Eve | Easter, VBS, separate adult/youth retreats, setup and teardown |

## Map only after coordinator review

1. Choose an example that matches the actual program. Adult hosting does not authorize youth supervision; a pantry helper does not prepare food. Keep childcare, transportation and specialist tasks separate.
2. Map each role name to the church's actual reviewed `Role` record. Existing roles such as `nursery`, `kids teacher`, `sound`, `first aid` and `driver` retain their current qualification requirements. New catalog roles are definitions to review, not granted clearances. Never replace a current role's requirements with weaker example requirements.
3. Define each qualification with the church or partner. `worship_team_clearance`, `group_host_clearance`, `food_safety_training`, `shelter_partner_orientation` and `setup_safety_training` are fictional policy identifiers. They do not establish legal sufficiency, completed training or suitability. Add actual site, safeguarding, food-service, accommodation, transport or equipment requirements where needed, and record administrator-verified evidence separately.
4. Review the stored `EventType` title patterns and `RoleRecipe` counts using the existing administrator configuration workflow. The examples use anchored `Example ...` titles to prevent matching real church events accidentally. Replace patterns only after checking them against the church's actual calendar and other configured types; the existing mapper selects the first matching type.
5. Set actual start/end times, program dates, staffing ratios and counts. Example counts are not minimum safe supervision ratios. Setup and teardown are separate timed events because the current recipe contract creates slots within an event's own time window.
6. Preserve the configured `fill_policy`. The examples hold worship, youth, group hosting, meal preparation, shelter support and setup/teardown roles for coordinator approval. Current, verified and unexpired qualifications remain prerequisites even when an administrator approves outreach. Runtime consent, eligibility, quiet hours and review guards continue to apply.

Recipe counts stored by the administrator govern slot creation. Loading a calendar event consults those stored rows, not this catalog. The existing mapper adds missing slots and preserves existing slots and assignment history; lowering a recipe count does not delete slots that already exist, so those changes need coordinator review. Reading these examples does not activate an import, change the default demo, schedule anyone, grant clearance or send a text.

## Verification limits

The focused synthetic tests map every optional type through the existing read-only calendar importer, verify slot counts and repeat-import deduplication, check that stored administrator counts prevail, and exercise missing, pending, expired and verified qualifications through the existing eligibility function. They also verify that the ordinary seed still loads only its original four event types.

This is source and configuration coverage. No real church calendar, live Planning Center staffing, transport delivery, actual administrator setup or credential verification is established by these tests. Live activation and proof remain separate.
