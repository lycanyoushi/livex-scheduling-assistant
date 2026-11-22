import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";

import Home from "../app/page";

it("renders a labeled chat shell with messaging disabled during setup", () => {
  const html = renderToStaticMarkup(createElement(Home));

  expect(html).toContain("<main ");
  expect(html).toContain('id="chat-title"');
  expect(html).toContain('for="chat-message"');
  expect(html).toMatch(/<textarea\b[^>]*\bdisabled=""/);
  expect(html).toMatch(/<button\b[^>]*\bdisabled=""[^>]*>Send<\/button>/);
});
