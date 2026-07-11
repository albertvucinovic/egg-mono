export type PerformanceFixtureSize = 0 | 100 | 300;

const MARKDOWN_BLOCK = [
  '## Streaming performance fixture',
  '',
  'A realistic paragraph with **bold**, _emphasis_, [a link](https://example.com), and `inline code`.',
  '',
  '```typescript',
  'export function boundedWork(value: string): number {',
  '  return value.split("\\n").reduce((total, line) => total + line.length, 0);',
  '}',
  '```',
  '',
  '$$\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}$$',
  '',
  '| invariant | result |',
  '| --- | --- |',
  '| chronology | preserved |',
  '| continuity | atomic |',
].join('\n');

export function performanceTranscript(size: PerformanceFixtureSize) {
  if (size === 0) return [];
  // About 6.8 KiB per message yields the investigation's ~2 MiB/300-message
  // case while retaining Markdown/code/math/tool-output coverage.
  const filler = size === 300 ? `\n\n${'Large realistic payload text with stable wrapping. '.repeat(145)}` : '';
  return Array.from({ length: size }, (_, index) => {
    if (index % 5 === 4) {
      return {
        id: `perf-tool-${index}`,
        role: 'tool',
        tool_call_id: `perf-call-${index}`,
        timestamp: new Date(1_700_000_000_000 + index * 1000).toISOString(),
        content: `tool output ${index}\n${'x'.repeat(size === 300 ? 6_500 : 1_200)}`,
      };
    }
    return {
      id: `perf-message-${index}`,
      role: index % 2 ? 'assistant' : 'user',
      timestamp: new Date(1_700_000_000_000 + index * 1000).toISOString(),
      content: `${MARKDOWN_BLOCK}${filler}`,
      ...(index % 10 === 1 ? {
        reasoning: `Reasoning ${index}: ${'bounded reasoning '.repeat(30)}`,
        tool_calls: [{ id: `perf-call-${index}`, name: 'bash', arguments: JSON.stringify({ script: `echo ${index}` }) }],
      } : {}),
    };
  });
}
