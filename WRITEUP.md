# LedgerIR — a zero-hallucination financial compiler for Indian MSMEs

*Submission for the AWS Builder Center Weekend Challenge: "No Pipeline, No Problem".*

**Live demo:** https://ledgerir.sumanbiswas.in — one click, no signup, full
results in a few seconds. (The dashboard is hosted on the project's S3 static
site; the custom domain is a thin HTTPS front on the author's own DNS.)

## The problem

India has ~6 crore registered MSMEs, and their paperwork lives in three worlds
that don't talk to each other: supplier invoices arrive as photos on WhatsApp,
the GST portal publishes a static GSTR-2B statement of what suppliers *claim*
they billed, and the bank statement shows lump-sum NEFT payments that settle
"two invoices minus a credit note" in one cryptic row. When these three
disagree, the MSME loses real money: blocked Input Tax Credit, "deemed
acceptance" of wrong invoices under the GST IMS regime, and interest on wrongly
availed ITC at 18% (as notified; statutory ceiling 24%).

The obvious 2026 answer — point an LLM at the documents — is exactly wrong for
this domain. A model that hallucinates one digit in a GSTIN or a tax amount
doesn't just make a typo; it files an incorrect statutory return.

## The idea

LedgerIR treats MSME paperwork as **untrusted source code** and compiles it.
Every document passes through a deterministic pipeline — OCR tokens → typed
invoice AST → GST rule checks → subset-sum bank reconciliation → IMS triage →
SHA-256 Merkle audit dossier — and **no LLM or managed AI API appears anywhere
in it**. Every output is explainable by a rule or an equation, and every
automatic correction ships with a machine-checkable proof.

Three design decisions carry the whole system:

**1. Two error classes, never mixed.** When a low-confidence OCR token breaks a
statutory invariant (`qty × price = line total`, `Σ lines = taxable value`,
`taxable × rate = tax`, GSTIN mod-36 checksum), the compiler enumerates every
confusion-variant of that token (8↔B, 3↔8, 1↔7, …) and accepts a repair **only
when exactly one candidate satisfies every invariant**. Zero or multiple
survivors → `NEEDS_REVIEW`, never a guess. That uniqueness rule is what makes
"zero hallucination" an honest claim rather than a slogan — and the emitted
proof object lists every candidate tried. A *supplier's* mistake (CGST+SGST
charged on an interstate bill, an invalid rate for the invoice date) is the
opposite case: the document is never altered, it's flagged as `SUPPLIER_ERROR`
and drives an automatic GST payment hold.

**2. Money is integer paise everywhere.** No float ever touches an amount, a
rate (basis points), or even a similarity score (also basis points). This
bought an unplanned prize: canonical JSON is byte-identical between Python and
JavaScript, so **the dashboard re-verifies the Merkle root in your browser** —
WebCrypto rebuilds all leaf hashes from the records rendered on the page and
recomputes the tree. Your browser independently checks the server's math.

**3. Determinism as a feature.** Same books ⇒ same root, across reruns *and
across environments*: the root computed by the deployed pipeline is
byte-identical to the one computed offline from the fixtures by the test suite.
The dossier is sealed in an S3 bucket with Object Lock in compliance mode —
genuinely undeletable until retention expires — giving an MSME a tamper-evident
"proof of cashflow" to hand a bank underwriter.

## What the demo shows

Click **Load Sample MSME Batch** and the pipeline processes a synthetic October
2026 batch end-to-end (measured Step Functions execution: **0.45–1.07 s** across
three runs):

1. A Gujarat→Kerala invoice whose rate digit is genuinely blurred in the image.
   OCR read it as "8" at 62% confidence; the compiler proves `3` is the only
   digit satisfying all invariants and repairs it — proof attached. The same
   bill wrongly charges CGST+SGST on an interstate supply; that is the
   supplier's legal error, so it is *flagged*, not fixed.
2. One NEFT debit of ₹93,220 is matched to INV-881 (₹59,000) + INV-883
   (₹35,400) − credit note CN-12 (₹1,180) by signed subset-sum search —
   residual **₹0.00**, found among decoy transactions.
3. The IMS decision table auto-accepts the three clean settled documents and
   issues `HOLD_GST` on the faulty bill: ₹7,200 of GST held back before the
   money leaves.
4. The Merkle root appears with a **"Re-verify in this browser"** button.

## Architecture

![architecture](architecture.svg)

| Subsystem | Service | Pattern |
|---|---|---|
| Documents & dossier vault | S3 (+ Object Lock) | claim-check store; write-once audit proofs |
| Ingestion & query API | API Gateway HTTP API | presigned uploads, demo seeder, dashboard reads |
| Orchestration | Step Functions Standard | Parallel(Map compile \| 2B \| bank) → Reconcile → Merkle; states carry S3 keys only |
| Solvers | Lambda arm64 / Python 3.12 | pure-Python cores with zero AWS imports |
| State | DynamoDB single table | one partition per tenant-month; records stored as canonical JSON |
| IaC | AWS SAM | one `sam build && sam deploy` from a laptop — no pipeline, no problem |

## Craft & honesty notes

- **58 offline tests** run with zero AWS dependencies: golden-sample assertions,
  schema validation (the JSON Schemas are extracted from SPEC.md itself, so the
  spec is machine-checked), independent reference oracles, meet-in-the-middle
  equivalence, and an end-to-end determinism gate.
- **OCR status, honestly:** the architecture reserves an arm64 container Lambda
  running `rapidocr-onnxruntime` as a pure spatial tokenizer. In the current
  deployment the demo invoice ships with its precomputed token file (the exact
  format the tokenizer emits — including the deliberate low-confidence
  misread), and the pipeline's Choice state routes `*_tokens.json` around the
  OCR stage; the placeholder OCR Lambda fails loudly rather than pretending.
  Everything downstream of tokens — the part with the novel claims — is fully
  real and running.
- **All data is synthetic.** GSTINs are generated with valid mod-36 checksums
  but belong to no real business; amounts, parties and bank rows are invented.
- The measured numbers above are from the deployed system in `ap-south-1`, not
  estimates.

## Why it matters

The factory-gate moment — an ops clerk photographing a supplier bill — is where
billing mistakes are cheapest to catch, 45 days before they become tax-filing
disputes. LedgerIR gives that moment a compiler: 3 seconds of deterministic
checking, an exception-only review queue for the owner, and a cryptographic
audit pack for the CA and the bank. Serverless is not incidental here: an MSME
batch is tiny and monthly, so the entire system idles at effectively zero cost
and scales per-invoice when the month closes.
