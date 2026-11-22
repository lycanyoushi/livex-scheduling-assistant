import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";

import Home from "../app/page";

it("renders the chat with an enabled, labeled composer and an empty draft", () => {
  const html = renderToStaticMarkup(createElement(Home));

  expect(html).toContain("<main ");
  expect(html).toContain('id="chat-title"');
  expect(html).toContain('for="chat-message"');
  expect(html).toContain('role="log"');
  expect(html).toMatch(/<textarea\b[^>]*\bid="chat-message"[^>]*>/);
  expect(html).not.toMatch(/<textarea\b[^>]*\bdisabled=""/);
  expect(html).toMatch(/<button\b[^>]*\btype="submit"[^>]*\bdisabled=""[^>]*>Send<\/button>/);
});
