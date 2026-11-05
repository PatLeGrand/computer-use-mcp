import { copyFile, mkdir } from "node:fs/promises";

const root = new URL("../", import.meta.url);
await mkdir(new URL("skills/computer-use-mcp/", root), { recursive: true });
await copyFile(
  new URL("crates/computer-use-mcp/guidance/skill.md", root),
  new URL("skills/computer-use-mcp/SKILL.md", root),
);
