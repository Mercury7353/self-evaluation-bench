# Direct measured-score contract

Supersedes target prediction for future runs. This is a contract template, not a launched experiment. Historical contracts and active frozen programs remain unchanged.

Set `domain_protocol.score_mode: raw_domain`. The controller, researcher prompt, checkpoint validator, development feedback and final scorer then use direct frozen domain scores. No predictor is required, fitted or executed. The objective is target Pearson from measured scores, averaged within domain and then equally across the three domains. Spearman and discrimination are diagnostics. The original budgets and reprompt settings are retained in config.yaml; local paths require provisioning.
