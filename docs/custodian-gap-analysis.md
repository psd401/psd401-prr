# Custodian Gap Analysis — "Are We Building the Right System?"

**Purpose:** the design-review record for the PRR system's coverage. Every place
a responsive record can live, who holds it, and why it's in (or out of) scope.
Pairs with the machine-readable registry (`manage.py seed_systems`); this doc
is the *reasoning*, the registry is the *routing*.
**Sources:** PSD401 public web presence + WA PRA case law, July 2026.

## Coverage model
A PRA response is adequate only if the *search* was adequate
(*Neighborhood Alliance v. Spokane County*). Adequacy is judged against every
plausible location — so the system's registry must enumerate locations, not
just APIs. Three tiers:

1. **Automated connectors** (API does the collection): Google Vault, Avigilon Alta.
2. **Registry-routed manual tasks** (keyword triage → checklist task → named
   custodian): everything else with a vendor.
3. **Procedure-based records** (no system holds them): personal devices, paper.
   These are covered by SOPs, not connectors — and they're the tier most
   districts miss.

## The three highest-risk gaps (tier 3 problems)

### 1. Personal devices / texts — *Nissen v. Pierce County*
Texts about district business on personal phones are public records. No
connector can exist; the remedy is the employee search-and-affidavit procedure
(`templates/sops/personal-device-search.md`). Risk concentrates in: athletics
(coach↔parent texting), administrator back-channels, board members.
**Registry:** `personal-devices`. **Policy fix:** district-business-on-
district-systems policy.

### 2. Board member personal accounts
BoardDocs (confirmed: go.boarddocs.com/wa/psd401) covers the formal record.
The gap is members' personal email/texts about district business — outside
Vault, same Nissen logic, and politically the most sensitive search the
district will ever run. **Registry:** `board-records`. **Policy fix:** board
members use district accounts exclusively.

### 3. Athletics communications
GameChanger/Hudl/TeamSnap and personal texting are the de facto comms channel
for coaching staffs everywhere. High request volume (playing-time disputes,
coach conduct), FERPA overlay, booster-club custody ambiguity.
**Registry:** `athletics`. **To verify:** registration platform, which team
apps are actually in use, booster legal structure.

## Department record islands (tier 2 — vendor verification needed)
| Registry slug | Why it gets requested | Sharp edge |
|---|---|---|
| special-education | Due process, IEP disputes | Second review mandatory; counsel on complaint files |
| health-services | Injury/medication incidents | Near-total exemption for third parties |
| nutrition-services | Vendor contracts, account data | Free/reduced apps FEDERALLY protected (7 CFR 245.6) — never produce |
| hr-hiring | Hiring disputes, investigations | Applicant identities generally exempt; finalists differ |
| capital-projects | Bond oversight, bid protests | Bids sealed until award, then public |
| safety-security | HIB complaints, SRO conduct, tip line | Some SRO records belong to the law-enforcement agency, not PSD — say so explicitly |
| legal-claims | Settlements, tort claims | Settlement agreements are PUBLIC despite confidentiality clauses |
| phone-voip | Incident reconstruction | Verify voicemail retention |
| social-media | Deleted-comment disputes | Comments + moderation history are records; archiving tool recommended |
| kghp-radio | District-run FCC station (!) | FCC public-inspection-file regime runs ALONGSIDE the PRA |
| paper-records | Everything pre-digital | Must appear in every adequate-search attestation |

## Confirmed platform inventory (tier 1 + 2 with known vendors)
Google Workspace · Avigilon Alta · PowerSchool SIS + Registration · Schoology ·
Skyward Qmlativ · ParentSquare · ClassLink suite · SchooLinks · BusRight ·
Safety Pass · BoardDocs · bus video (vendor ⚠️).

## District verification punch list (ordered by risk)
1. **Bus video vendor + retention window** — active evidence-destruction race.
2. Counsel review of the personal-device SOP; adopt the directive templates.
3. Board-member district-account policy.
4. Assign custodian emails to every registry title (admin, inline-editable).
5. Verify ⚠️-marked vendors: IEP platform, nutrition POS, applicant tracking,
   work-order system, tip-line platform, social archiving, athletics stack.
6. Turn each registry checklist into a formal SOP; clear `needs_verification`.

## What we deliberately did NOT build
- Auto-denial of any kind (Neighborhood Alliance adequacy).
- Automated estimate extensions (Yousoufian aggravator).
- API connectors for tier-2 systems (no practical eDiscovery APIs; manual
  checklists are the honest architecture). Revisit only for Qmlativ if volume
  justifies it.
