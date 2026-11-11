import { createHash } from "node:crypto";
import { copyFile, mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const [output, bundles] = process.argv.slice(2);
const firmware = join(output, "firmware");
const image = "airkeep-factory.bin";
const assets = [image, "SHA256SUMS.txt", "FLASHING.md"];
const stableTag = /^v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/;
const tags = bundles
  ? (await readdir(bundles, { withFileTypes: true }))
    .filter((entry) => entry.isDirectory() && stableTag.test(entry.name))
    .map((entry) => entry.name)
    .sort((first, second) => second.localeCompare(first, "en", { numeric: true }))
    .slice(0, 2)
  : [];

for (const tag of tags) {
  const source = join(bundles, tag);
  const destination = join(firmware, tag);
  const digest = createHash("sha256").update(await readFile(join(source, image))).digest("hex");
  const checksum = await readFile(join(source, "SHA256SUMS.txt"), "utf8");
  if (checksum.trimEnd() !== `${digest}  ${image}`) throw new Error(`Firmware checksum mismatch for ${tag}`);
  await mkdir(destination, { recursive: true });
  for (const asset of assets) await copyFile(join(source, asset), join(destination, asset));
  const manifest = {
    name: "Airkeep",
    version: tag.slice(1),
    new_install_prompt_erase: false,
    new_install_improv_wait_time: 0,
    builds: [{ chipFamily: "ESP32-S3", parts: [{ path: "./" + image, offset: 0 }] }],
  };
  await writeFile(join(destination, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
}

await mkdir(firmware, { recursive: true });
await writeFile(join(firmware, "release.json"), JSON.stringify({ version: tags[0]?.slice(1) ?? null }) + "\n");
