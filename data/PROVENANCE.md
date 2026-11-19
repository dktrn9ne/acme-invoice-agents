# Invoice corpus provenance

The files in `data/invoices/` and `data/generate_pdfs.py` are copied from the public Galatiq case repository:

- Source: https://github.com/galatiq-ai/galatiq-case-invoices
- Retrieved: 2026-11-19
- License: the case repository does not declare a license. These files are included so reviewers can run the assessment against the original fixtures. They are not original work.

`generate_pdfs.py` is the upstream helper that produced `invoice_1011.pdf`, `invoice_1012.pdf`, and `invoice_1013.pdf`. It is unchanged.

The directory contains 20 invoice files (text, JSON, CSV, XML, and PDF). The case README refers to this folder as the sample set; count them here rather than assuming a round number.

Do not "clean up" typos, totals, or addresses in these files. They are the traps the pipeline is graded on.
