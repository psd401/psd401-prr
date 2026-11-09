// ABOUTME: The trust footer's default facts for staff screens, kept apart from the
// ABOUTME: component so tests and screens can name them without breaking fast refresh.

/**
 * The default facts are true of every staff screen today: the workspace shows
 * district records to signed-in staff, nothing it drafts reaches a requester
 * until a person approves it, and outside the two automated paths compliance
 * rule 6 allows, it sends nothing on its own.
 */
export const STAFF_TRUST_FACTS = [
  'Staff only',
  'District records, handle under RCW 42.56 and FERPA',
  'Drafts are not sent until a person approves them',
]
