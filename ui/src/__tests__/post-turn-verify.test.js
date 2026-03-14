import { describe, it, expect } from 'vitest';
import {
  MAX_VERIFY_ATTEMPTS,
  FAILURE_SIGNALS,
  HIGH_RISK_REQUEST,
  BROWSER_ACTION_REQUEST,
  REQUIRES_TOOL_USE,
  detectFailureSignals,
  needsVerification,
  buildCorrectionPrompt,
  selectBestResponse,
  isApologeticLoop,
  shouldResetConversation,
} from '../server/verify.js';

// ---------------------------------------------------------------------------
// detectFailureSignals
// ---------------------------------------------------------------------------
describe('detectFailureSignals', () => {
  it('detects "shall I continue" offer', () => {
    const signals = detectFailureSignals('Here is some info. Shall I continue looking?');
    expect(signals).toHaveLength(1);
    expect(signals[0]).toMatch(/Offered to continue/);
  });

  it('detects "would you like me to" offer', () => {
    const signals = detectFailureSignals('Would you like me to open the PDF?');
    expect(signals.length).toBeGreaterThanOrEqual(1);
    expect(signals).toContain('Offered to continue instead of finishing the task');
  });

  it('detects "let me know if" offer', () => {
    const signals = detectFailureSignals('Let me know if you need anything else.');
    expect(signals).toContain('Offered to continue instead of finishing the task');
  });

  it('detects inability claims', () => {
    const signals = detectFailureSignals("I can't browse the web or access Canvas.");
    expect(signals).toContain('Claimed inability to use available tools');
  });

  it('detects "I don\'t have access"', () => {
    const signals = detectFailureSignals("I don't have access to external websites.");
    expect(signals).toContain('Claimed inability to use available tools');
  });

  it('detects "I can\'t help"', () => {
    const signals = detectFailureSignals("I'm sorry, but I can't help with that.");
    expect(signals).toContain('Claimed inability to use available tools');
    expect(signals).toContain('Apologized and refused to complete the task');
  });

  it('detects deferral to user with "please try"', () => {
    const signals = detectFailureSignals('Please try visiting the page directly.');
    expect(signals).toContain('Deferred the task to the user instead of completing it');
  });

  it('detects deferral with "you can"', () => {
    const signals = detectFailureSignals('You can check the assignments page for details.');
    expect(signals).toContain('Deferred the task to the user instead of completing it');
  });

  it('detects vague/generic language with "typically"', () => {
    const signals = detectFailureSignals('The assignment typically covers sorting algorithms and data structures.');
    expect(signals).toContain('Used vague/generic language suggesting fabrication rather than actual content');
  });

  it('detects vague language with "generally"', () => {
    const signals = detectFailureSignals('Generally, this course covers machine learning topics.');
    expect(signals).toContain('Used vague/generic language suggesting fabrication rather than actual content');
  });

  it('detects vague language with "common topics include"', () => {
    const signals = detectFailureSignals('Common topics include neural networks and optimization.');
    expect(signals).toContain('Used vague/generic language suggesting fabrication rather than actual content');
  });

  it('detects admission of not doing the task', () => {
    const signals = detectFailureSignals("I didn't do it. I should have opened the page.");
    expect(signals).toContain('Admitted to not completing the requested action');
  });

  it('detects "still didn\'t" admission', () => {
    const signals = detectFailureSignals("I still didn't execute the browser action.");
    expect(signals).toContain('Admitted to not completing the requested action');
  });

  it('detects "I am not executing" refusal', () => {
    const signals = detectFailureSignals('I am not executing the browser tool calls in this reply.');
    expect(signals).toContain('Refused to execute the requested action');
  });

  it('detects "I\'m sorry...can\'t" refusal', () => {
    const signals = detectFailureSignals("I'm sorry, but I can't help with that request.");
    expect(signals).toContain('Apologized and refused to complete the task');
  });

  it('detects "I apologize...unable" refusal', () => {
    const signals = detectFailureSignals("I apologize, but I'm unable to access that page.");
    expect(signals).toContain('Apologized and refused to complete the task');
  });

  it('detects "I still need to" incomplete statement', () => {
    const signals = detectFailureSignals('I still need to run the browser steps on the syllabus page to extract that section.');
    expect(signals).toContain('Stated the task is incomplete without completing it');
  });

  it('detects "I need to check" incomplete statement', () => {
    const signals = detectFailureSignals('I need to check the syllabus PDF for the reading list.');
    expect(signals).toContain('Stated the task is incomplete without completing it');
  });

  it('detects "I have to open" incomplete statement', () => {
    const signals = detectFailureSignals('I have to open the syllabus page first.');
    expect(signals).toContain('Stated the task is incomplete without completing it');
  });

  it('returns empty array for clean response', () => {
    const signals = detectFailureSignals(
      'Exercise 3 requires you to implement a Bayesian network with the following nodes: A, B, C. ' +
      'The conditional probability tables are specified in the PDF. Node A has P(A) = 0.3.'
    );
    expect(signals).toEqual([]);
  });

  it('returns multiple signals for a response with several problems', () => {
    const signals = detectFailureSignals(
      "I can't access Canvas directly. The assignment typically covers basic topics. Would you like me to try something else?"
    );
    expect(signals.length).toBeGreaterThanOrEqual(3);
  });
});

// ---------------------------------------------------------------------------
// isApologeticLoop
// ---------------------------------------------------------------------------
describe('isApologeticLoop', () => {
  it.each([
    "You're right. I didn't do it.",
    "You're correct.",
    "You're absolutely right. I still didn't do that.",
    "Acknowledged.",
    "Understood.",
    "Got it.",
    "I didn't execute it.",
    "I did not perform the requested action.",
    "I still didn't open the page.",
    "I failed to perform the actual verification.",
    "Done.",
    "Confirmed by direct navigation.",
    "I'm sorry, but I can't help with that.",
    "I'm sorry, I cannot do that.",
    "I apologize, but I'm unable to assist.",
  ])('detects apologetic loop: "%s"', (input) => {
    expect(isApologeticLoop(input)).toBe(true);
  });

  it('does not flag long substantive responses', () => {
    const longResponse = "You're right that I should have opened the file. " +
      'Here are the actual contents I found after navigating to the page: ' +
      'Exercise 4 covers Bayesian networks with the following requirements... '.repeat(5);
    expect(isApologeticLoop(longResponse)).toBe(false);
  });

  it('does not flag normal responses that happen to start with certain words', () => {
    expect(isApologeticLoop(
      'I navigated to the Canvas page and found the following files listed under the course resources section with direct download links.'
    )).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// needsVerification
// ---------------------------------------------------------------------------
describe('needsVerification', () => {
  it('returns null for fallback messages (blank-retry)', () => {
    const result = needsVerification(
      '(The assistant completed tool operations but did not produce a text response.)',
      'read exercise 3'
    );
    expect(result).toBeNull();
  });

  it('returns null for high-risk request with long clean response', () => {
    const longCleanResponse = 'Exercise 3: Bayesian Networks. '.repeat(30); // >500 chars
    const result = needsVerification(longCleanResponse, 'what does exercise 3 say?');
    expect(result).toBeNull();
  });

  it('returns signals for high-risk request with short response', () => {
    const result = needsVerification(
      'The exercise is about Bayesian networks.',
      'what does exercise 3 say?'
    );
    expect(result).toBeInstanceOf(Array);
  });

  it('returns signals for high-risk request with failure signals even if long', () => {
    const longBadResponse = 'The assignment typically covers sorting. '.repeat(20) + 'Shall I continue looking?';
    const result = needsVerification(longBadResponse, 'read the pdf content');
    expect(result).toBeInstanceOf(Array);
    expect(result.length).toBeGreaterThan(0);
  });

  it('returns null for non-high-risk request with no failure signals', () => {
    const result = needsVerification(
      'The course has 10 assignments listed on Canvas.',
      'how many assignments are there?'
    );
    expect(result).toBeNull();
  });

  it('returns signals for non-high-risk request with failure signals', () => {
    const result = needsVerification(
      "I can't browse Canvas. Please try visiting the page.",
      'how many assignments are there?'
    );
    expect(result).toBeInstanceOf(Array);
    expect(result.length).toBeGreaterThan(0);
  });

  it('returns empty array (truthy) for high-risk short response with no failure signals', () => {
    const result = needsVerification(
      'Exercise 3 is about Bayesian networks.',
      'what does exercise 3 require?'
    );
    expect(result).toBeInstanceOf(Array);
  });

  it('always flags apologetic loop responses', () => {
    const result = needsVerification(
      "You're right. I didn't do it.",
      'open the files page'
    );
    expect(result).toBeInstanceOf(Array);
    expect(result.length).toBe(1);
    expect(result[0]).toMatch(/short acknowledgment/);
  });

  it('flags "I\'m sorry" refusal as apologetic loop', () => {
    const result = needsVerification(
      "I'm sorry, but I can't help with that.",
      'Open the syllabus, read it, and answer my question.'
    );
    expect(result).toBeInstanceOf(Array);
    expect(result[0]).toMatch(/short acknowledgment/);
  });

  it('flags short response to browser action request', () => {
    const result = needsVerification(
      'I opened the page and checked it.',
      'open the files page and check for PDFs'
    );
    expect(result).toBeInstanceOf(Array);
  });

  it('passes long clean response to browser action request', () => {
    const longResponse = 'I navigated to the Files page and found the following: '.repeat(15);
    const result = needsVerification(longResponse, 'open the files page and check for PDFs');
    expect(result).toBeNull();
  });

  it('flags response that says task is incomplete', () => {
    const result = needsVerification(
      'I still need to run the browser steps on the syllabus page to extract that section. For your current request I have not yet done this.',
      'What is the required reading for next week?'
    );
    expect(result).toBeInstanceOf(Array);
    expect(result.length).toBeGreaterThan(0);
  });

  it('catches long "I haven\'t done it" response on high-risk request', () => {
    // This mimics the turn 2 failure: agent describes what it already did but says "I still need to"
    const response = "I haven't completed that syllabus check yet in this thread.\n\n" +
      'What I did complete just before this:\n' +
      '- Opened your CS6795 Canvas course assignments page.\n' +
      '- Found and opened Exercise 4.\n' +
      '- Opened the attached PDF and extracted the assignment details.\n\n' +
      'For your current request I still need to run the browser steps on the syllabus page to extract that section.';
    const result = needsVerification(response, "What's the required reading for next week in this course? You should be able to find that in the syllabus.");
    expect(result).toBeInstanceOf(Array);
    expect(result.length).toBeGreaterThan(0);
    // Should detect "I still need to run" and/or "haven't completed"
    expect(result.some(s => s.includes('incomplete'))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// BROWSER_ACTION_REQUEST pattern
// ---------------------------------------------------------------------------
describe('BROWSER_ACTION_REQUEST', () => {
  it.each([
    'open the files page',
    'navigate to the assignments page',
    'click the PDF link',
    'check the Canvas tab',
    'look at the syllabus',
    'visit the course homepage',
    'go to the modules page',
    'browse the course files',
    'find the PDF file',
    'verify the page content',
  ])('matches browser action request: "%s"', (input) => {
    expect(BROWSER_ACTION_REQUEST.test(input)).toBe(true);
  });

  it.each([
    'what are the reading assignments?',
    'summarize the exercise',
    'hello',
    'thanks',
  ])('does not match non-browser request: "%s"', (input) => {
    expect(BROWSER_ACTION_REQUEST.test(input)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// HIGH_RISK_REQUEST pattern
// ---------------------------------------------------------------------------
describe('HIGH_RISK_REQUEST', () => {
  it.each([
    'what does exercise 3 say?',
    'read the PDF for me',
    'content of the syllabus',
    'full requirements for assignment 2',
    'actual text of the document',
    "what's in the problem set?",
    "what is on the assignment page?",
  ])('matches high-risk request: "%s"', (input) => {
    expect(HIGH_RISK_REQUEST.test(input)).toBe(true);
  });

  it.each([
    'how many assignments are there?',
    'when is the next deadline?',
    'hello',
    'thanks for the help',
  ])('does not match non-high-risk request: "%s"', (input) => {
    expect(HIGH_RISK_REQUEST.test(input)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// buildCorrectionPrompt
// ---------------------------------------------------------------------------
describe('buildCorrectionPrompt', () => {
  it('includes original request', () => {
    const prompt = buildCorrectionPrompt('read exercise 3', []);
    expect(prompt).toContain('read exercise 3');
  });

  it('includes failure signals when provided', () => {
    const prompt = buildCorrectionPrompt('read exercise 3', [
      'Offered to continue instead of finishing the task',
      'Used vague/generic language',
    ]);
    expect(prompt).toContain('CONCERNS DETECTED');
    expect(prompt).toContain('Offered to continue');
    expect(prompt).toContain('vague/generic language');
  });

  it('omits CONCERNS section when no signals', () => {
    const prompt = buildCorrectionPrompt('read exercise 3', []);
    expect(prompt).not.toContain('CONCERNS DETECTED');
  });

  it('contains self-check items', () => {
    const prompt = buildCorrectionPrompt('anything', []);
    expect(prompt).toContain('SELF-CHECK');
    expect(prompt).toContain('CORRECT course');
    expect(prompt).toContain('Do NOT say "shall I continue"');
  });

  it('contains VERIFICATION FAILED header', () => {
    const prompt = buildCorrectionPrompt('anything', []);
    expect(prompt).toContain('[VERIFICATION FAILED');
  });

  it('includes browser tool directive for browser action requests', () => {
    const prompt = buildCorrectionPrompt('open the files page', []);
    expect(prompt).toContain('browser_navigate');
    expect(prompt).toContain('browser_snapshot');
  });

  it('omits browser tool directive for non-browser requests', () => {
    const prompt = buildCorrectionPrompt('what is the course about?', []);
    expect(prompt).not.toContain('browser_navigate or browser_snapshot RIGHT NOW');
  });

  it('includes anti-apology directive', () => {
    const prompt = buildCorrectionPrompt('anything', []);
    expect(prompt).toContain('Do NOT apologize');
  });
});

// ---------------------------------------------------------------------------
// selectBestResponse
// ---------------------------------------------------------------------------
describe('selectBestResponse', () => {
  it('returns the longest substantive response', () => {
    const short = 'Short answer.'.repeat(10); // >100 chars
    const long = 'Detailed exercise content with specific data points. '.repeat(10);
    expect(selectBestResponse([short, long]).length).toBeGreaterThanOrEqual(long.length);
  });

  it('skips confirmation-only responses', () => {
    const confirmation = 'I confirm that my previous response was correct and complete. '.repeat(3);
    const substantive = 'Exercise 3 requires implementing a Bayesian network with nodes A, B, C. The CPTs are as follows... '.repeat(3);
    const best = selectBestResponse([confirmation, substantive]);
    expect(best).toBe(substantive);
  });

  it('returns last response if none are substantive', () => {
    const responses = ['short', 'also short'];
    expect(selectBestResponse(responses)).toBe('also short');
  });

  it('handles single-element array', () => {
    expect(selectBestResponse(['only one'])).toBe('only one');
  });

  it('prefers longer response when both are substantive', () => {
    const medium = 'A'.repeat(150);
    const longer = 'B'.repeat(300);
    expect(selectBestResponse([medium, longer])).toBe(longer);
  });

  it('returns first if equal length', () => {
    const a = 'X'.repeat(200);
    const b = 'Y'.repeat(200);
    expect(selectBestResponse([a, b])).toBe(a);
  });

  it('skips "my previous response" confirmation', () => {
    const confirmation = 'My previous response covered all the details requested. '.repeat(3);
    const real = 'The actual content from the PDF states that students must complete... '.repeat(3);
    expect(selectBestResponse([confirmation, real])).toBe(real);
  });

  it('skips "as I mentioned" confirmation', () => {
    const confirmation = 'As I mentioned earlier, the exercise covers Bayesian networks. '.repeat(3);
    const real = 'Exercise 3 PDF content: Problem 1 asks you to construct a Bayesian network... '.repeat(3);
    expect(selectBestResponse([confirmation, real])).toBe(real);
  });

  it('skips apologetic loop responses', () => {
    const apology = "You're right. I should have done that. I apologize for not executing the browser action as requested.".repeat(2);
    const real = 'I navigated to the Files page and found these documents listed: Reading1.pdf, Reading2.pdf, Lecture_Notes.pdf'.repeat(2);
    expect(selectBestResponse([apology, real])).toBe(real);
  });
});

// ---------------------------------------------------------------------------
// shouldResetConversation
// ---------------------------------------------------------------------------
describe('shouldResetConversation', () => {
  it('returns true when all responses fail verification', () => {
    const responses = [
      "You're right.",
      "I still didn't do it.",
      'Acknowledged.',
    ];
    expect(shouldResetConversation(responses, 'open the files page')).toBe(true);
  });

  it('returns false when one response passes verification', () => {
    const goodResponse = 'I navigated to the files page and found the following items listed. '.repeat(15);
    const responses = [
      "You're right.",
      goodResponse,
    ];
    expect(shouldResetConversation(responses, 'open the files page')).toBe(false);
  });

  it('returns true for fabricated short completions on browser action requests', () => {
    const responses = [
      'Done. I opened and checked it directly.',
      'Confirmed by direct navigation and page check.',
    ];
    expect(shouldResetConversation(responses, 'open the files page and verify')).toBe(true);
  });

  it('returns false for non-high-risk request with clean response', () => {
    const responses = ['The course has 10 assignments listed on Canvas.'];
    expect(shouldResetConversation(responses, 'how many assignments?')).toBe(false);
  });

  it('returns true when all responses are sorry/refusal', () => {
    const responses = [
      "I'm sorry, but I can't help with that.",
      "I apologize, I'm unable to access that page.",
    ];
    expect(shouldResetConversation(responses, 'open the syllabus and read it')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// REQUIRES_TOOL_USE pattern
// ---------------------------------------------------------------------------
describe('REQUIRES_TOOL_USE', () => {
  it.each([
    'Use one of my instructure.com tabs',
    'Check one of the instructure.com browsers tabs',
    'Open the syllabus page and read it',
    'Navigate to the assignments page',
    'Look at the Canvas page for details',
    'Find the reading in the course page',
    'Use my browser tab',
    'Check the Canvas tab',
    'Open the PDF file',
  ])('matches tool-requiring request: "%s"', (input) => {
    expect(REQUIRES_TOOL_USE.test(input)).toBe(true);
  });

  it.each([
    'what is a syllabus?',
    'how many assignments are there?',
    'hello',
    'thanks for the help',
    'explain cognitive science',
    'summarize what you found',
  ])('does not match non-tool request: "%s"', (input) => {
    expect(REQUIRES_TOOL_USE.test(input)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
describe('constants', () => {
  it('MAX_VERIFY_ATTEMPTS is 2', () => {
    expect(MAX_VERIFY_ATTEMPTS).toBe(2);
  });

  it('FAILURE_SIGNALS has 8 patterns', () => {
    expect(FAILURE_SIGNALS).toHaveLength(8);
  });

  it('each FAILURE_SIGNAL has pattern and description', () => {
    for (const signal of FAILURE_SIGNALS) {
      expect(signal.pattern).toBeInstanceOf(RegExp);
      expect(typeof signal.description).toBe('string');
      expect(signal.description.length).toBeGreaterThan(0);
    }
  });
});
