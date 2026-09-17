# Experiment record

The analysis plan is in PROTOCOL.md. Default retrieval parameters were fixed before running public-data scores.

The first public-data run stopped after the 75-instance progress checkpoint because the released LongMemEval history contains an empty message. The product rejects empty evidence. The dataset adapter was corrected to count and skip empty source messages regardless of labels, while preserving any gold annotations in the scorer's denominator. No query, method, score threshold, model or retrieval parameter changed. The complete evaluation was restarted after this adapter correction. The partial run was not used in reported aggregate results.

Operational tests and external retrieval tests are separate. Internal examples were used to fix implementation defects before the public-data evaluation. No semantic model was trained or evaluated. No external evaluation label was used to select a task, add aliases, extract facts, expand a query or rank evidence.
