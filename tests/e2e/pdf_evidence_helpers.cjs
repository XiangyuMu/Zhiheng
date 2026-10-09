const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { execFileSync } = require("node:child_process");

function sha256Buffer(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

function sha256File(file) {
  return sha256Buffer(fs.readFileSync(file));
}

function normalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function privateTextProof(value) {
  const normalized = normalizeText(value);
  return {
    sha256: sha256Buffer(Buffer.from(normalized, "utf8")),
    length: normalized.length,
  };
}

function siblingFailurePath(output, scriptName) {
  const resolved = path.resolve(output);
  const parent = path.dirname(resolved);
  const name = path.basename(resolved);
  if (!fs.existsSync(parent)) return null;
  return path.join(parent, `${name}.${scriptName}.failure-${Date.now()}.json`);
}

function temporaryFailurePath(scriptName) {
  return path.join(os.tmpdir(), `zhiheng-${scriptName}-failure-${Date.now()}.json`);
}

function initializeEvidence(scriptName, output, report) {
  let outputReady = false;
  let setupError = null;
  let outputPath = output ? path.resolve(output) : null;
  let reportPath = output ? siblingFailurePath(output, scriptName) : null;
  if (!reportPath) reportPath = temporaryFailurePath(scriptName);

  if (!output) {
    setupError = "Expected output directory";
  } else if (fs.existsSync(outputPath)) {
    setupError = `output directory already exists: ${output}`;
  } else {
    try {
      fs.mkdirSync(outputPath, { mode: 0o700 });
      outputReady = true;
      reportPath = path.join(outputPath, "report.json");
    } catch (error) {
      setupError = `cannot create output directory: ${error.message}`;
    }
  }

  if (setupError) report.setup_error = setupError;
  report.evidence = {
    output_dir: outputPath,
    report_path: reportPath,
    output_ready: outputReady,
  };

  const save = () => {
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2), { mode: 0o600 });
  };
  return { outputReady, outputPath, reportPath, setupError, save };
}

function expectationSha(expectation) {
  return expectation.sha256 || expectation.source_sha256 || expectation.pdf_sha256;
}

function expectationQuote(expectation) {
  return expectation.quote || expectation.expected_quote;
}

function expectationPage(expectation) {
  const page = expectation.page_no ?? expectation.expected_page_no ?? expectation.page;
  return page == null ? null : Number(page);
}

function loadExpectation(expectationPath, actualSha) {
  if (!expectationPath) return null;
  const expectation = JSON.parse(fs.readFileSync(expectationPath, "utf8"));
  const expectedSha = expectationSha(expectation);
  assert(expectedSha, "expectation JSON must include sha256/source_sha256/pdf_sha256");
  assert.equal(expectedSha, actualSha, "expectation JSON must be bound to the input PDF SHA");
  const page = expectationPage(expectation);
  const quote = expectationQuote(expectation);
  assert(Number.isInteger(page) && page > 0, "expectation JSON must include a positive page_no");
  assert(quote && normalizeText(quote), "expectation JSON must include quote");
  return { ...expectation, sha256: expectedSha, page_no: page, quote };
}

function validateExpectationAgainstPdf(pdfPath, expectation) {
  if (!pdfPath || !expectation) return null;
  const code = [
    "import json, re, sys",
    "from pypdf import PdfReader",
    "payload = json.load(sys.stdin)",
    "reader = PdfReader(sys.argv[1], strict=False)",
    "page_no = int(payload['page_no'])",
    "quote = re.sub(r'\\s+', ' ', payload['quote']).strip()",
    "if page_no < 1 or page_no > len(reader.pages):",
    "    raise SystemExit(f'expected page {page_no} outside PDF page count {len(reader.pages)}')",
    "text = reader.pages[page_no - 1].extract_text() or ''",
    "normalized = re.sub(r'\\s+', ' ', text).strip()",
    "if quote not in normalized:",
    "    raise SystemExit('expected quote was not found on the expected PDF page')",
    "print(json.dumps({'page_no': page_no, 'page_count': len(reader.pages), 'quote_length': len(quote)}))",
  ].join("\n");
  return JSON.parse(
    execFileSync("uv", ["run", "python", "-c", code, pdfPath], {
      encoding: "utf8",
      input: JSON.stringify({
        page_no: expectation.page_no,
        quote: expectation.quote,
      }),
    }),
  );
}

function sanitizedExpectation(expectation, localPdfCheck) {
  if (!expectation) return { status: "not_provided" };
  return {
    status: "loaded",
    sha256: expectation.sha256,
    page_no: expectation.page_no,
    quote: privateTextProof(expectation.quote),
    local_pdf_check: localPdfCheck,
  };
}

async function verifyClickedCitation({ page, base, reader, expectation, expectedPdfSha }) {
  const citations = page.locator('[data-reader-citation="true"]');
  await citations.first().waitFor({ state: "visible", timeout: 20_000 });
  const citationCount = await citations.count();
  let selected = citations.first();
  if (expectation) {
    let pageMatch = null;
    let selectedByQuote = false;
    for (let index = 0; index < citationCount; index += 1) {
      const candidate = citations.nth(index);
      const text = await candidate.innerText();
      const pageMatches = text.includes(`第 ${expectation.page_no} 页`);
      if (pageMatches && normalizeText(text).includes(normalizeText(expectation.quote))) {
        selected = candidate;
        selectedByQuote = true;
        break;
      }
      if (pageMatches && pageMatch === null) pageMatch = candidate;
    }
    if (!selectedByQuote && pageMatch !== null) {
      selected = pageMatch;
    }
  }
  await selected.click();
  await page.locator("#citation-context").waitFor({ state: "visible", timeout: 10_000 });

  const quote = page.locator('#citation-context-text [data-reader-quote="true"]');
  await quote.waitFor({ state: "visible", timeout: 10_000 });
  const actualQuote = await quote.innerText();
  assert(normalizeText(actualQuote), "reader citation must show the original quote");
  if (expectation) {
    assert.equal(
      normalizeText(actualQuote),
      normalizeText(expectation.quote),
      "clicked citation quote must match external PDF expectation",
    );
  }

  const pageLink = page.locator("#citation-page-link");
  await pageLink.waitFor({ state: "visible", timeout: 10_000 });
  const href = await pageLink.getAttribute("href");
  assert(href, "reader citation must expose an authenticated original PDF link");
  assert.match(href, /\/v1\/knowledge\/[^/]+\/export\?/, "citation link must use export API");
  assert.match(href, /format=original/, "citation link must request original PDF");
  assert.match(href, /disposition=inline/, "citation link must request inline preview");
  if (expectation) assert(href.endsWith(`#page=${expectation.page_no}`), "citation link page must match expectation");
  assert.equal(await pageLink.getAttribute("target"), "_blank");

  const original = await page.request.get(new URL(href, base).href);
  assert.equal(original.status(), 200);
  assert.match(original.headers()["content-type"] || "", /application\/pdf/i);
  const pdfBytes = await original.body();
  const returnedSha = sha256Buffer(pdfBytes);
  assert.equal(returnedSha, expectedPdfSha, "authenticated original PDF digest must match expectation");

  return {
    clicked: true,
    citation_count: citationCount,
    page_link: href,
    page_link_status: original.status(),
    returned_pdf_sha256: returnedSha,
    quote: privateTextProof(actualQuote),
  };
}

module.exports = {
  initializeEvidence,
  loadExpectation,
  normalizeText,
  privateTextProof,
  sanitizedExpectation,
  sha256Buffer,
  sha256File,
  validateExpectationAgainstPdf,
  verifyClickedCitation,
};
