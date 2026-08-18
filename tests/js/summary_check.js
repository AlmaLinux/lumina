/*
 * Checks for submission-summary.js, run by
 * lumina/results/tests/test_submission_summary.py under node.
 *
 * The pure functions only. Wiring is asserted at source level from the Python side, the
 * same split combobox_check.js uses, because the interesting part here is the *wording* of
 * consequences and getting one of those backwards is how a summary lies to a submitter.
 */
const assert = require("assert");
const summary = require("../../static/js/submission-summary.js");

let checks = 0;
function check(name, fn) {
  fn();
  checks += 1;
}

// --- matchesKnown --------------------------------------------------------------

check("an exact name matches", () => {
  assert.strictEqual(summary.matchesKnown("Dell Inc.", ["Dell Inc."]), true);
});

check("case and surrounding space do not matter", () => {
  assert.strictEqual(summary.matchesKnown("  dell inc.  ", ["Dell Inc."]), true);
});

check("a longer model is not a match", () => {
  // The reason this is equality and not a prefix test: R750 and R750xd are different
  // machines, and reporting one as the other would merge two listings.
  assert.strictEqual(summary.matchesKnown("PowerEdge R750xd", ["PowerEdge R750"]), false);
});

check("empty is never a match", () => {
  assert.strictEqual(summary.matchesKnown("", ["Dell Inc."]), false);
  assert.strictEqual(summary.matchesKnown("   ", ["Dell Inc."]), false);
});

check("no known values means nothing matches", () => {
  assert.strictEqual(summary.matchesKnown("Dell", undefined), false);
});

// --- fieldChange ---------------------------------------------------------------

check("an untouched field produces no line", () => {
  assert.strictEqual(summary.fieldChange("Name", "R750", "R750"), null);
});

check("whitespace-only edits are not changes", () => {
  assert.strictEqual(summary.fieldChange("Name", "R750", "  R750 "), null);
});

check("a real edit reports both sides", () => {
  const change = summary.fieldChange("Name", "R750", "R760");
  assert.strictEqual(change.kind, "changed");
  assert.strictEqual(change.from, "R750");
  assert.strictEqual(change.to, "R760");
});

check("filling an empty field reads as added", () => {
  assert.strictEqual(summary.fieldChange("Description", "", "2U server").kind, "added");
});

check("blanking a filled field reads as cleared, not as an edit", () => {
  // Blank means "no change" on the server (apply_vendor_maintained_fields skips it), so
  // the summary must not promise an erase.
  assert.strictEqual(summary.fieldChange("Description", "2U server", "").kind, "cleared");
});

// --- releaseEffect -------------------------------------------------------------

check("a major the listing does not claim is new", () => {
  assert.strictEqual(summary.releaseEffect(10, undefined, false).kind, "new");
});

check("an already-claimed release gains this person's confirmation", () => {
  assert.strictEqual(summary.releaseEffect(9, { source: "run" }, false).kind, "attest");
});

check("a release this person already confirmed is a no-op", () => {
  // Attestations are one per (version, person), so a second run of theirs adds evidence
  // but not a second confirmation. Claiming otherwise would inflate the count in the
  // reader's head.
  assert.strictEqual(summary.releaseEffect(9, { source: "run" }, true).kind, "noop");
});

check("the minor a run passed on does not enter the claim", () => {
  // Hardware certifies per major. There used to be a fourth outcome, `widen`, for a run
  // that lowered a per-major minor floor, and three checks here asserted the floor
  // arithmetic. A run on 9.4 and a run on 9.8 now say exactly the same thing about
  // AlmaLinux 9, and the minor lives on the run's own record.
  const claimed = { source: "run" };
  assert.strictEqual(
    summary.releaseEffect(9, claimed, false).kind,
    summary.releaseEffect(9, claimed, false).kind,
  );
  assert.ok(!("floor" in summary.releaseEffect(9, claimed, false)));
  assert.ok(!("was" in summary.releaseEffect(9, claimed, false)));
});

// --- summarize -----------------------------------------------------------------

const NEW_LISTING = { listing: null, versions: {}, components: [], known: {} };

check("a new listing says so first", () => {
  const lines = summary.summarize(NEW_LISTING, {
    vendor_name: "Dell Inc.", name: "PowerEdge R760", releases: [], excluded: [],
  });
  assert.strictEqual(lines[0].tone, "new");
  assert.ok(lines[0].text.includes("PowerEdge R760"));
});

const EXISTING = {
  listing: {
    label: "Dell Inc. PowerEdge R760", name: "PowerEdge R760",
    model_number: "R760", description: "Old text", vendor_spec_url: "",
  },
  versions: { 9: { mine: false, attestations: 1, source: "run" } },
  components: [],
  known: {},
};

check("an existing listing is not described as created", () => {
  const lines = summary.summarize(EXISTING, {
    name: "PowerEdge R760", releases: [], excluded: [],
  });
  assert.strictEqual(lines[0].tone, "info");
  assert.ok(!lines.some((l) => (l.text || "").includes("Creates a new catalog listing")));
});

check("only edited fields appear", () => {
  const lines = summary.summarize(EXISTING, {
    name: "PowerEdge R760", model_number: "R760", description: "New text",
    releases: [], excluded: [],
  });
  const changes = lines.filter((l) => l.change).map((l) => l.change.label);
  assert.deepStrictEqual(changes, ["Description"]);
});

check("a field absent from the form is never diffed", () => {
  // description is dropped for a community submitter on a re-validation. Treating
  // "not on the form" as "cleared" would announce an erase nobody asked for.
  const lines = summary.summarize(EXISTING, { releases: [], excluded: [] });
  assert.strictEqual(lines.filter((l) => l.change).length, 0);
});

check("an excluded component is reported as excluded", () => {
  const baseline = Object.assign({}, NEW_LISTING, {
    components: [{
      key: "gpu:l40s", kind: "gpu", kind_label: "GPU", label: "NVIDIA L40S",
      will_create: true, new_vendor: false, matches: "",
    }],
  });
  const lines = summary.summarize(baseline, {
    vendor_name: "x", name: "y", releases: [], excluded: ["gpu:l40s"],
  });
  const excluded = lines.filter((l) => l.tone === "excluded");
  assert.strictEqual(excluded.length, 1);
  assert.ok(excluded[0].text.includes("will not be added"));
});

check("a matched component names what it attaches to", () => {
  const baseline = Object.assign({}, NEW_LISTING, {
    components: [{
      key: "cpu:xeon", kind: "cpu", kind_label: "CPU", label: "Intel Xeon Gold 6430",
      will_create: false, new_vendor: false, matches: "Xeon Scalable 4th Generation",
    }],
  });
  const lines = summary.summarize(baseline, {
    vendor_name: "x", name: "y", releases: [], excluded: [],
  });
  const match = lines.find((l) => l.tone === "match");
  assert.ok(match.text.includes("Xeon Scalable 4th Generation"));
});

check("a new component warns about a new manufacturer too", () => {
  const baseline = Object.assign({}, NEW_LISTING, {
    components: [{
      key: "mb:x", kind: "motherboard", kind_label: "Motherboard", label: "OEM 0M83RH",
      will_create: true, new_vendor: true, matches: "",
    }],
  });
  const lines = summary.summarize(baseline, {
    vendor_name: "x", name: "y", releases: [], excluded: [],
  });
  assert.ok(lines.some((l) => (l.text || "").includes("new manufacturer")));
});

console.log(`submission-summary: ${checks} checks passed`);
