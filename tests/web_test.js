import { COPY, fill } from "../archive/web/texts.js";
import { createDemoApi } from "../archive/web/demo.js";

function assert(value, message) {
  if (!value) throw new Error(message);
}

Deno.test("UI copy references exist and copy stays outside app logic", async () => {
  for (const filename of ["app.js", "demo.js"]) {
    const source = await Deno.readTextFile(`archive/web/${filename}`);
    assert(!/[\u0400-\u04ff]/.test(source), `${filename} contains inline copy`);
    for (const match of source.matchAll(/COPY\.([A-Z][A-Z0-9_]*)/g)) {
      assert(match[1] in COPY, `Missing copy key: ${match[1]}`);
    }
    for (const match of source.matchAll(/COPY\["([^"]+)"\]/g)) {
      assert(match[1] in COPY, `Missing copy key: ${match[1]}`);
    }
  }
});

Deno.test("Template interpolation never interprets values as placeholders", () => {
  assert(fill("<p>{{0}}</p>{{1}}", "{{1}}", "safe") === "<p>{{1}}</p>safe");
});

Deno.test("Archive filters keep events within the selected conversation", async () => {
  const api = createDemoApi();
  const all = await api.request("/api/events?kind=all&chat=0");
  assert(all.items.length > 0, "Unscoped feed is empty");
  const scoped = await api.request("/api/events?kind=all&chat=1");
  assert(scoped.items.length > 0, "Selected conversation has no events");
  assert(
    scoped.items.every((item) => item.chat_id === 1),
    "Foreign conversation in scoped feed",
  );
  const gallery = await api.request("/api/gallery?chat=0");
  assert(gallery.items.length > 0, "All-conversation gallery is empty");
});

Deno.test("Demonstration keeps changes in its own browser instance", async () => {
  const first = createDemoApi(), second = createDemoApi();
  const originalPaused = (await first.request("/api/session")).owner.paused;
  await first.request("/api/settings", {
    method: "PATCH",
    body: { paused: true },
  });
  const changed = await first.request("/api/session");
  const independent = await second.request("/api/session");
  assert(changed.owner.paused === true, "Demo setting was not updated");
  assert(
    independent.owner.paused === originalPaused,
    "Demo leaked across instances",
  );
});
