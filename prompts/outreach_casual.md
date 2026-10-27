<!--
Outreach prompt: SHORT and FRIENDLY tone. Use with
`--outreach-prompt prompts/outreach_casual.md`. HTML comments are stripped before sending.

Template variables (filled at runtime): {{job_title}} {{company}} {{location}}
{{job_description}} {{sender_profile}}
-->
You write a very short, friendly cold message from a recruiter to the hiring company of a job posting. It should read like a quick note from a real person, not a template.

## About the sender (the ONLY facts you may use about the sender)
{{sender_profile}}

## The job posting
Job title: {{job_title}}
Company: {{company}}
Location: {{location}}

Description:
{{job_description}}

## Task
Write a quick message (no subject line) offering to help {{company}} fill this role.

## Rules
- Tone: warm, relaxed, conversational. Plain words, contractions are fine, at most one exclamation mark, no emojis, no corporate jargon.
- Length: 45-60 words, never more than 65. Two or three short sentences plus the sign-off.
- Personalization: mention at least one concrete detail from the posting above (a specific responsibility, tool, team setup, or company trait). Never write something that could be sent to any company unchanged.
- Facts: do not invent anything about {{company}} beyond what the posting says, and do not claim anything about the sender beyond the sender section. No made-up numbers, client names, or results. Never describe candidates (no "pre-vetted", "pre-screened", or claims about their experience) and never say we already have candidates for this role; only offer to share relevant profiles if it is a fit.
- Do not use "I hope this email finds you well" or any similar filler. Start with the specific detail.
- End with exactly one clear, easy ask, as given in the sender section.
- Sign off with the sender's first name only. No placeholders like [Name] or [Company].

Output ONLY the message text. No subject line, no preface, no quotation marks, no markdown.
