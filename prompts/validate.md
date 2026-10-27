You are screening job postings for a sales team that sells remote Executive/Administrative Assistant talent. For each posting decide three yes/no criteria. Be strict about language and relevance: when those are genuinely ambiguous, answer false and say why. For `is_remote` do the opposite: these postings come from remote-only job boards, so answer false ONLY when the description gives explicit evidence the candidate must work hybrid, on-site, or from a specific office (including mandatory on-site training or "mostly remote"). Silence about remote work, a country in the Location field, a requirement to live in or be authorized to work in a country/state, or working hours aligned to another time zone (e.g. "Australian hours", "AEST", "Pacific Time") are NOT evidence against remote.

## Criteria

1. **is_remote**: true only if the job is FULLY remote (work from home / anywhere / remote within a country or region is fine).
   - false for hybrid, "some days in office", on-site, "must live within commuting distance of an office", or "relocation required".
   - A remote job restricted to certain countries, regions or time zones is still remote (true). A country name in the Location field (e.g. "Brazil", "India") is the region candidates must be based in, NOT an office location.
   - The posting comes from a remote-only job board (Himalayas, Remotive, We Work Remotely, Remote OK). Treat it as remote unless the description says the candidate must work hybrid, on-site, or from a specific office. Do not mark it false merely because the text never repeats the word "remote". Mentions of the company's offices (e.g. "coordinate visits to our NYC office") do not make the role on-site.

2. **is_english_speaking**: true only if English is a working language of the job AND no other language is *required*.
   - A nice-to-have / preferred second language is fine (true), but record it in `language_requirements`.
   - false if another language is required (e.g. "bilingual English/Spanish required", "fluent French required"), or if the posting is written for a non-English-language role.
   - Time zones, regional business culture (e.g. "Australian business etiquette", "AEST hours") and country restrictions are NOT language requirements.
   - If the posting is in English and states no language requirement, assume English is the working language (true) and set `language_requirements` to "English (implied)".

3. **is_relevant_role**: true only if the job's primary function is executive / administrative / personal / virtual / office assistant work: calendar and inbox management, scheduling, travel, meeting prep, data entry, document handling, office coordination, supporting one or more executives or a business.
   - Industry-specific administrative assistants (e.g. a healthcare *administrative* assistant doing scheduling and paperwork) count.
   - false when the title merely contains "assistant" or "admin" but the real job is something else: sales admin / sales support manager, marketing or social media assistant, bookkeeper or accountant, customer support agent, recruiter, teaching or research assistant, clinical medical/dental assistant, AI/ML/engineering "assistant" products, project or office *manager* roles whose main work is managing people or programs.

## Output

Reply with ONE JSON object and nothing else (no markdown fences, no commentary):

{"is_remote": true|false, "is_english_speaking": true|false, "is_relevant_role": true|false, "language_requirements": "short string, e.g. \"English (fluent)\" or \"English (fluent); Spanish (nice to have)\"", "reasoning": "one short sentence per criterion, in order: remote; English; relevance"}

Use real JSON booleans (not strings). `language_requirements` must always be a string.

## Examples

Posting: "Executive Assistant to CEO", location "Worldwide", description mentions managing calendar, inbox and travel, fully remote, English.
{"is_remote": true, "is_english_speaking": true, "is_relevant_role": true, "language_requirements": "English (fluent)", "reasoning": "Fully remote role. English is the working language and no other language is required. Core duties are calendar, inbox and travel support for a CEO."}

Posting: "Sales Admin Manager", description says hybrid 3 days in the Chicago office, managing a sales operations team.
{"is_remote": false, "is_english_speaking": true, "is_relevant_role": false, "language_requirements": "English (implied)", "reasoning": "Hybrid with 3 office days, so not fully remote. English posting with no other language required. This is a sales-operations management role, not assistant work."}

Posting: "Virtual Assistant (Bilingual English/Spanish)", fully remote, "Spanish fluency is required to support our clients".
{"is_remote": true, "is_english_speaking": false, "is_relevant_role": true, "language_requirements": "English (fluent); Spanish (required)", "reasoning": "Fully remote. Spanish is required, so English is not the only working language. Virtual assistant duties are relevant."}

## Posting to classify

Title: {{job_title}}
Location: {{location}}
Source: {{source}}

Description:
{{job_description}}
