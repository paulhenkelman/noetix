// Post-turn verification helpers for detecting incomplete/inadequate agent responses

import config from './config.js';

export const MAX_VERIFY_ATTEMPTS = config.maxVerifyAttempts;

export const FAILURE_SIGNALS = [
  { pattern: /\b(shall I|should I|would you like me to|want me to|if you('d| would) like|I can also|let me know if)\b/i,
    description: 'Offered to continue instead of finishing the task' },
  { pattern: /\b(I (don't have|don't|can't|cannot|am unable to|do not have) (access|browse|read|view|open|navigate|help))\b/i,
    description: 'Claimed inability to use available tools' },
  { pattern: /\b(please (try|check|visit|open|go to)|you (can|could|should|may want to|might want to))\b/i,
    description: 'Deferred the task to the user instead of completing it' },
  { pattern: /\b(typically|generally|usually|in most cases|it('s| is) likely|probably covers|common topics include)\b/i,
    description: 'Used vague/generic language suggesting fabrication rather than actual content' },
  { pattern: /\b(I (didn't|did not|have not|haven't|still didn't|still have not) (do it|done it|execute|perform|open|complet|actually|verify|navigate|check))\b/i,
    description: 'Admitted to not completing the requested action' },
  { pattern: /\b(I (am not|was not|will not) execut)/i,
    description: 'Refused to execute the requested action' },
  { pattern: /(I'?m sorry|I apologize).{0,30}(can't|cannot|unable|don't|won't|will not|not able)/i,
    description: 'Apologized and refused to complete the task' },
  { pattern: /\b(I (still need|need|have) to (run|check|open|navigate|look|browse|complete|do|verify|fetch|extract|read|pull))\b/i,
    description: 'Stated the task is incomplete without completing it' },
];

export const HIGH_RISK_REQUEST = /\b(what does|read the|content of|text of|full (requirements|details|content|text)|actual (content|text|requirements)|exercise\b|assignment\b|pdf\b|syllabus|problem set|what('s| is) (in|on) the)\b/i;

export const BROWSER_ACTION_REQUEST = /\b(open|navigate|click|check|look at|visit|go to|browse|find .*(page|tab|link|file)|verify .*(page|tab|file)|tab)\b/i;

// Requests that explicitly reference browser/Canvas resources and thus REQUIRE tool use.
// More targeted than HIGH_RISK_REQUEST — specifically about interacting with web pages.
export const REQUIRES_TOOL_USE = /\b(instructure|canvas.*(tab|page|site)|browser.*(tab|page)|my tab|open.*(page|tab|pdf|syllabus|file|link)|navigate to|check.*(tab|page|canvas)|find.*(in|on).*(canvas|course|page|site)|use.*(tab|browser)|look.*(at|in).*(tab|page|canvas))\b/i;

export function detectFailureSignals(responseText) {
  const detected = [];
  for (const signal of FAILURE_SIGNALS) {
    if (signal.pattern.test(responseText)) {
      detected.push(signal.description);
    }
  }
  return detected;
}

/**
 * Detect if a response is part of an apologetic/acknowledgment loop.
 * These are short, non-action responses where the model acknowledges
 * failure, apologizes, or refuses without actually doing anything.
 */
export function isApologeticLoop(responseText) {
  const trimmed = responseText.trim();
  if (trimmed.length > 300) return false;
  return /^(you'?re (right|correct|absolutely right)|acknowledged|understood|got it|I'?m sorry|I apologize|I (didn't|did not|have not|haven't|am not|cannot|still didn't|failed to|will not)|done\.|doing it|confirmed)/i.test(trimmed);
}

export function needsVerification(responseText, originalRequest) {
  // Never verify fallback messages from the blank-retry
  if (responseText.startsWith('(The assistant completed tool operations')) return null;

  // Always flag apologetic loop responses
  if (isApologeticLoop(responseText)) {
    return ['Response is a short acknowledgment/apology without completing the task'];
  }

  const signals = detectFailureSignals(responseText);

  // Browser action requests: verify unless response is long + clean
  if (BROWSER_ACTION_REQUEST.test(originalRequest)) {
    if (signals.length === 0 && responseText.trim().length > 500) return null;
    return signals.length > 0 ? signals : [];
  }

  // High-risk content requests: always verify unless response is long + clean
  if (HIGH_RISK_REQUEST.test(originalRequest)) {
    if (signals.length === 0 && responseText.trim().length > 500) return null;
    return signals;
  }

  // Non-high-risk: verify only if failure signals detected
  return signals.length > 0 ? signals : null;
}

export function buildCorrectionPrompt(originalRequest, failureSignals) {
  const signalSection = failureSignals.length
    ? `\nCONCERNS DETECTED:\n${failureSignals.map(s => `- ${s}`).join('\n')}`
    : '';

  // For browser-action requests, include explicit tool-call instructions
  const isBrowserAction = BROWSER_ACTION_REQUEST.test(originalRequest);
  const toolDirective = isBrowserAction
    ? `\nYou MUST call browser_navigate or browser_snapshot RIGHT NOW. Do not respond with text alone. Call the browser tool first, then summarize what you see.`
    : '';

  return `[VERIFICATION FAILED — COMPLETE THE TASK]
The user's original request was: "${originalRequest}"

Your previous response did not fully satisfy this request.${signalSection}${toolDirective}

SELF-CHECK:
1. Did you actually retrieve and read the specific content the user asked about, or did you only report metadata (title, filename, due date)?
2. Did you complete the ENTIRE task, or did you stop partway and offer to continue?
3. Did you verify you were looking at the CORRECT course/page/document?
4. If you provided specific details, are they from actual content you read, or are they vague/generic?

If any check fails: use your tools NOW to complete the task. Navigate, click, read, extract.
If your previous response was complete: restate the key findings (include actual content, not just "I confirm").

Do NOT say "shall I continue" — just do it. Do NOT claim tools are unavailable. Do NOT apologize or acknowledge — act.`;
}

export function selectBestResponse(responses) {
  const CONFIRMATION_ONLY = /^(I confirm|my previous response|as I (mentioned|stated)|the information I provided)/i;
  const substantive = responses.filter(r =>
    r.trim().length > 100 && !CONFIRMATION_ONLY.test(r.trim()) && !isApologeticLoop(r)
  );
  if (substantive.length === 0) return responses[responses.length - 1];
  return substantive.reduce((a, b) => a.length >= b.length ? a : b);
}

/**
 * Check if all responses from the verification loop are inadequate,
 * indicating the conversation is degraded and needs a reset.
 */
export function shouldResetConversation(allResponses, originalRequest) {
  // If any response passes verification, no reset needed
  for (const response of allResponses) {
    if (!needsVerification(response, originalRequest)) return false;
  }
  return true;
}
