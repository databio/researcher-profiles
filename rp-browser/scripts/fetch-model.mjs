// Download the quantized ONNX model for local embedding.
// Run with: npm run fetch:model
//
// The model files are gitignored and not committed. A fresh clone needs
// network once. Set VITE_RP_ALLOW_REMOTE_MODELS=1 to skip this and pull
// from the Hub at runtime instead.
import { mkdirSync, existsSync, writeFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const modelDir = resolve(here, "..", "public", "models", "Xenova", "all-MiniLM-L6-v2");

const FILES = [
  {
    name: "config.json",
    url: "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/config.json",
  },
  {
    name: "tokenizer.json",
    url: "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer.json",
  },
  {
    name: "tokenizer_config.json",
    url: "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/tokenizer_config.json",
  },
  {
    name: "onnx/model_quantized.onnx",
    url: "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/main/onnx/model_quantized.onnx",
  },
];

mkdirSync(resolve(modelDir, "onnx"), { recursive: true });

for (const file of FILES) {
  const outPath = resolve(modelDir, file.name);
  if (existsSync(outPath)) {
    console.log(`[fetch-model] ${file.name} already exists, skipping`);
    continue;
  }

  console.log(`[fetch-model] downloading ${file.name}...`);
  try {
    const res = await fetch(file.url);
    if (!res.ok) {
      console.error(`[fetch-model] HTTP ${res.status} for ${file.url}`);
      continue;
    }
    const buf = Buffer.from(await res.arrayBuffer());
    writeFileSync(outPath, buf);
    console.log(`[fetch-model] wrote ${outPath} (${buf.length} bytes)`);
  } catch (e) {
    console.error(`[fetch-model] failed to download ${file.name}: ${e}`);
  }
}

console.log("[fetch-model] done");
