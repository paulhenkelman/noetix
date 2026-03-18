/**
 * Fallback prompted tool calling for models without native tool support.
 *
 * Injects tool definitions into the system prompt and parses
 * <tool_call> blocks from model output.
 */

/**
 * Append tool definitions to a system message.
 */
export function injectToolPrompt(systemContent, tools) {
  if (!tools || tools.length === 0) return systemContent;

  const toolDefs = tools.map((t) => {
    const schema = JSON.stringify(t.inputSchema || {}, null, 2);
    return `### ${t.name}\n${t.description || 'No description.'}\nParameters:\n\`\`\`json\n${schema}\n\`\`\``;
  }).join('\n\n');

  const injection = `\n\n---\n\nYou have access to the following tools. To use a tool, output a <tool_call> block:\n\n<tool_call>{"name": "tool_name", "arguments": {...}}</tool_call>\n\nYou may call multiple tools in one response. After all tool results are returned, continue your response.\n\nAvailable tools:\n\n${toolDefs}`;

  return systemContent + injection;
}

/**
 * Extract tool calls from model output text.
 * Returns an array of { name, arguments } objects.
 */
export function parseToolCalls(text) {
  const results = [];
  const regex = /<tool_call>([\s\S]*?)<\/tool_call>/g;
  let match;

  while ((match = regex.exec(text)) !== null) {
    const raw = match[1].trim();
    try {
      const parsed = JSON.parse(raw);
      if (parsed.name) {
        results.push({
          name: parsed.name,
          arguments: parsed.arguments || {},
        });
      }
    } catch {
      // Malformed tool call — skip
    }
  }

  return results;
}

/**
 * Validate that a tool call references a known tool.
 */
export function validateToolCall(tc, tools) {
  const known = new Set(tools.map((t) => t.name));
  return known.has(tc.name);
}
