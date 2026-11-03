/**
 * Normalize LLM output before Markdown parsing so math renders reliably.
 *
 * remark-math only understands $...$ and $$...$$. Models also emit \(...\) and
 * \[...\], and plain dollar signs for money. This pass runs outside code, so
 * code blocks and inline code are never touched.
 *
 * 1. \[...\] -> $$...$$ and \(...\) -> $...$
 * 2. Currency dollars are escaped so they are not read as math delimiters:
 *    - a dollar after a currency prefix: R$ 50, US$ 10
 *    - a dollar before an amount followed by a non-math boundary: $5, $1,200.50
 *    "$2x$" or "$5$" stay math, because the amount is followed by a letter or "$".
 */

// Fenced code blocks (``` or ~~~) and inline code spans.
const CODE = /(```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)|`[^`\n]*`)/g;

const DISPLAY = /\\\[([\s\S]+?)\\\]/g;
const INLINE = /\\\(([\s\S]+?)\\\)/g;
const CURRENCY_PREFIX = /\b(R|US|A|C|NZ|HK|S|MX)\$/g;
const CURRENCY_AMOUNT = /(^|[^\\$\w])\$(?=\d[\d.,]*(?:[\s.,;:!?)\]]|$))/gm;

function normalizeText(text: string): string {
  return text
    .replace(DISPLAY, (_, body) => `\n$$\n${body.trim()}\n$$\n`)
    .replace(INLINE, (_, body) => `$${body.trim()}$`)
    .replace(CURRENCY_PREFIX, (_, prefix) => `${prefix}\\$`)
    .replace(CURRENCY_AMOUNT, (_, before) => `${before}\\$`);
}

export function normalizeMath(markdown: string): string {
  // split with a capturing group keeps the code segments at odd indices.
  return markdown
    .split(CODE)
    .map((part, i) => (i % 2 === 1 ? part : normalizeText(part)))
    .join("");
}
