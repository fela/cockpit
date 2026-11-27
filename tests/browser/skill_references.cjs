// A skill's reference files on the Skills page (SPEC SKILLS-1): the row of the skill's files, a link in the text
// opening a reference file at its section, and the way back to SKILL.md, at desktop and phone width.
// node tests/browser/skill_references.cjs
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { startPreview } = require("./work.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const SKILL = `---
name: tasks
description: Keep the Tasks app's lists in order.
---

# Tasks

Sort a list as [the steps](references/steps.md#second-part) say, and share it as in [sharing](references/sharing.md).
`;

const STEPS = `# Steps

## First part

Open the list. Back to [the skill](../SKILL.md).

${"Each reminder keeps its own time and its own list.\n\n".repeat(30)}
## Second part

Sort the reminders by time, then see [sharing](sharing.md).
`;

const SHARING = `# Sharing

Share a list with its owner's friends.
`;

// The harness's skills checkout (serve_slow.py) is a folder per skill under skills-repo/skills; the page reads it
// on every request, so it can be written after the server is up.
function seed(work) {
  const skill = path.join(work, "skills-repo", "skills", "tasks");
  fs.mkdirSync(path.join(skill, "references"), { recursive: true });
  fs.writeFileSync(path.join(skill, "SKILL.md"), SKILL);
  fs.writeFileSync(path.join(skill, "references", "steps.md"), STEPS);
  fs.writeFileSync(path.join(skill, "references", "sharing.md"), SHARING);
}

async function files(page) {
  return page.locator(".skill-files a").evaluateAll((links) => links.map((link) => ({
    text: link.textContent, href: link.getAttribute("href"), current: link.getAttribute("aria-current"),
  })));
}

(async () => {
  const { work, info } = await startPreview("cockpit-skill-references-");
  seed(work);
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined });
  try {
    for (const [width, height] of [[1440, 900], [390, 844]]) {
      const context = await browser.newContext({ viewport: { width, height } });
      await context.addCookies([{ name: info.name, value: info.value, domain: new URL(info.origin).hostname,
        path: "/", secure: true, httpOnly: true, sameSite: "Lax" }]);
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });

      await page.goto(info.origin + "/skills/tasks", { waitUntil: "networkidle" });
      assert.deepEqual(await files(page), [
        { text: "SKILL.md", href: "/skills/tasks", current: "page" },
        { text: "sharing.md", href: "/skills/tasks/references/sharing.md", current: null },
        { text: "steps.md", href: "/skills/tasks/references/steps.md", current: null },
      ]);
      console.log(`pass: ${width} px: the skill lists its files, SKILL.md open`);

      await page.locator("#doc .prose a", { hasText: "the steps" }).click();
      await page.waitForURL("**/skills/tasks/references/steps.md#second-part");
      await page.locator("#second-part").waitFor();
      assert.equal(await page.title(), "Cockpit tasks steps.md");
      assert.equal((await files(page)).find((file) => file.current === "page").text, "steps.md");
      await page.waitForFunction(() => {
        const box = document.getElementById("second-part").getBoundingClientRect();
        return box.top >= 0 && box.bottom <= innerHeight;
      });
      console.log(`pass: ${width} px: a link in the text opens the reference file at its section`);

      assert.equal(await page.locator("#doc .prose a", { hasText: "sharing" }).getAttribute("href"), "/skills/tasks/references/sharing.md");
      assert.equal(await page.locator("#doc .prose a", { hasText: "the skill" }).getAttribute("href"), "/skills/tasks");
      await page.screenshot({ path: path.join(work, `skill-reference-${width}.png`) });

      await page.locator(".skill-files a", { hasText: "SKILL.md" }).click();
      await page.waitForURL("**/skills/tasks");
      await page.locator("#doc h1", { hasText: "Tasks" }).waitFor();
      console.log(`pass: ${width} px: the file row leads back to SKILL.md`);

      assert.deepEqual(errors, []);
      const outside = await page.evaluate(() => fetch("/api/skills/tasks/references/..%2FSKILL.md").then((response) => response.status));
      assert.equal(outside, 404);
      console.log(`pass: ${width} px: a path out of references/ is refused`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
