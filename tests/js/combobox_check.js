/**
 * Match-quality check for static/js/combobox.js.
 *
 * There is no JS test runner in this project, so this is a plain node script:
 * `node tests/js/combobox_check.js`. It exists because the ranking is the whole
 * value of the widget and "looks about right" is not a test - each case below
 * is a string the collector actually reports paired with what the catalog
 * actually holds.
 */
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const { comboRank, comboSimilarity, comboVisible, comboInputClass } =
  require("../../static/js/combobox.js");

const VENDORS = ["AMD", "ASRock", "Community-Submitted", "Dell Inc.", "Intel",
                 "Lenovo", "NVIDIA", "Supermicro", "Hewlett Packard Enterprise"];
const CPUS = ["Core i3-10100T", "Xeon Gold 6430", "EPYC 7343", "Xeon Gold 5420+"];
const BOARDS = ["0M3F6C", "0M83RH", "21K9001NUS", "B650M PG Riptide"];

let failures = 0;
function expectTop(label, query, options, wanted) {
  const hits = comboRank(query, options);
  const top = hits.length ? hits[0].value : "(none)";
  const ok = top === wanted;
  if (!ok) failures += 1;
  console.log(
    (ok ? "ok   " : "FAIL ")
    + JSON.stringify(query).padEnd(36)
    + "-> " + top.padEnd(30)
    + (ok ? "" : `(wanted ${JSON.stringify(wanted)})`)
  );
}

console.log("--- the collector's spelling vs the catalog's ---");
// lscpu decorates with trademark marks the catalog does not carry.
expectTop("lscpu CPU", "Intel(R) Xeon(R) Gold 6430", CPUS, "Xeon Gold 6430");
// DMI vendor strings are routinely shorter or longer than the vendor record.
expectTop("short vendor", "Dell", VENDORS, "Dell Inc.");
expectTop("long vendor", "Dell Inc", VENDORS, "Dell Inc.");
expectTop("punctuated", "Dell, Inc.", VENDORS, "Dell Inc.");
// An acronym shares no words and almost no character pairs with its
// expansion, so this needs the initialism rule specifically.
expectTop("acronym", "HPE", VENDORS, "Hewlett Packard Enterprise");
expectTop("acronym reversed", "Hewlett Packard Enterprise",
          ["HPE", "Dell Inc."], "HPE");
expectTop("cpu with suffix", "AMD EPYC 7343 16-Core Processor", CPUS, "EPYC 7343");
expectTop("typo", "B650M PG Ritpide", BOARDS, "B650M PG Riptide");
expectTop("spacing", "B650M-PG Riptide", BOARDS, "B650M PG Riptide");
expectTop("substring", "6430", CPUS, "Xeon Gold 6430");

console.log("\n--- discrimination: near numbers must not be confused ---");
const g6430 = comboSimilarity("Intel(R) Xeon(R) Gold 6430", "Xeon Gold 6430");
const g5420 = comboSimilarity("Intel(R) Xeon(R) Gold 6430", "Xeon Gold 5420+");
console.log("  6430 -> 'Xeon Gold 6430' %s, 'Xeon Gold 5420+' %s",
            g6430.toFixed(2), g5420.toFixed(2));
assert(g6430 > g5420, "the right model must outrank a sibling");

console.log("\n--- honesty: no invention for genuinely unrelated strings ---");
for (const junk of ["OEM", "To Be Filled By O.E.M.", "7D2XCTO1WW"]) {
  const hits = comboRank(junk, VENDORS);
  console.log("  " + JSON.stringify(junk).padEnd(26)
              + `-> ${hits.length} suggestion(s) `
              + hits.slice(0, 2).map(h => h.value).join(", "));
}

console.log("\n--- empty query offers everything, for browsing ---");
assert.equal(comboRank("", VENDORS).length, VENDORS.length);

console.log("\n--- fuzzy hits are labeled, literal ones are not ---");
const fuzzy = comboRank("Intel(R) Xeon(R) Gold 6430", CPUS)[0];
const literal = comboRank("Xeon Gold", CPUS)[0];
console.log("  contained-in-query literal=%s, substring literal=%s",
            fuzzy.literal, literal.literal);
assert.equal(literal.literal, true, "a substring match is what was typed");

console.log("\n--- pinned entries survive the cap ---");
// The publisher pickers end with "+ Propose a new vendor…" and the menu caps at
// twelve. An empty query preserves the server's order, so before pinning that entry
// fell off the bottom the moment a thirteenth vendor existed - and the only way to
// reach the inline-vendor flow was to guess that typing "propose" found it.
const MANY = Array.from({ length: 30 }, (_, i) =>
  ({ value: "v" + i, label: "Publisher " + String(i).padStart(2, "0") }));
const PICKER = MANY.concat([{ value: "__new__", label: "+ Propose a new vendor\u2026" }]);

function expectEq(label, actual, wanted) {
  const ok = JSON.stringify(actual) === JSON.stringify(wanted);
  if (!ok) failures += 1;
  console.log((ok ? "ok   " : "FAIL ") + label.padEnd(54)
    + (ok ? "" : `got ${JSON.stringify(actual)} wanted ${JSON.stringify(wanted)}`));
}

expectEq("pinned action survives the cap",
  comboVisible("", PICKER, ["__new__"], 12).at(-1).value, "__new__");
expectEq("the cap still applies to ordinary choices",
  comboVisible("", PICKER, ["__new__"], 12).length, 13);
// "qqqq", not "no such publisher": the ranking is deliberately generous, and a query
// containing the word "publisher" legitimately matches every option here. That is the
// similarity matcher working, not a bug - it cost this assertion one revision.
expectEq("a query matching nothing still offers the action",
  comboVisible("qqqq", PICKER, ["__new__"], 12).map(o => o.value),
  ["__new__"]);
expectEq("searching for the action does not list it twice",
  comboVisible("propose", PICKER, ["__new__"]).filter(o => o.value === "__new__").length, 1);
expectEq("a real match ranks first, the action stays last",
  [comboVisible("Publisher 07", PICKER, ["__new__"], 12)[0].label,
   comboVisible("Publisher 07", PICKER, ["__new__"], 12).at(-1).value],
  ["Publisher 07", "__new__"]);
expectEq("no pins configured behaves exactly as before",
  comboVisible("", MANY, [], 12).length, 12);
expectEq("a pin naming a value not in the list is ignored",
  comboVisible("", MANY, ["__missing__"], 12).length, 12);

console.log("\n--- the search box does not look like a dropdown ---");
// Copying the select's class gave the input Bootstrap's .form-select, which paints a
// caret. The box then looked exactly like a native dropdown, which is why the first
// report on this feature was "it's not clear that you can type in the box".
expectEq("form-select becomes form-control",
  comboInputClass("form-select").split(" ").includes("form-select"), false);
expectEq("and gains the text-input class",
  comboInputClass("form-select").split(" ").includes("form-control"), true);
expectEq("size modifiers are translated, not dropped",
  comboInputClass("form-select form-select-sm"),
  "form-control form-control-sm combobox-input");
expectEq("an already-correct class is left alone",
  comboInputClass("form-control"), "form-control combobox-input");
expectEq("a bare select still gets a usable class",
  comboInputClass(""), "form-control combobox-input");
expectEq("unrelated classes survive",
  comboInputClass("custom-thing").split(" ").includes("custom-thing"), true);
expectEq("every box is marked for the search styling",
  comboInputClass("form-select").split(" ").includes("combobox-input"), true);

console.log("\n--- the DOM code actually uses the tested helpers ---");
// comboInputClass and comboVisible are pure so they can be tested here, but a pure
// helper nobody calls is worse than none: reverting setupSelect to
// `input.className = select.className` restored the caret bug with every check above
// still green. setupSelect needs a DOM, so this asserts the wiring at the source
// level - the only thing that can see it without a browser.
const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "..", "static", "js", "combobox.js"), "utf8");
const setupSelectBody = SOURCE.slice(
  SOURCE.indexOf("function setupSelect"),
  SOURCE.indexOf("function setupPicker"));

expectEq("setupSelect derives the input class via comboInputClass",
  setupSelectBody.includes("comboInputClass(select.className)"), true);
expectEq("setupSelect does not copy the select's class directly",
  /input\.className\s*=\s*select\.className/.test(setupSelectBody), false);
expectEq("setupSelect builds its menu via comboVisible",
  setupSelectBody.includes("comboVisible("), true);
expectEq("setupSelect follows a pinned option's target",
  setupSelectBody.includes("followPin("), true);
expectEq("the search box is an input type=search",
  /input\.type\s*=\s*"search"/.test(setupSelectBody), true);

console.log(failures ? `\n${failures} FAILURE(S)` : "\nall ranking checks passed");
process.exit(failures ? 1 : 0);
