import { useEffect, useRef, useState, type FormEvent } from "react";

type Asset = { width: number; height: number; format: string; license_status: string; ai_generated: boolean; alt_text: string };
type Rendition = { image_id: string; article_version: number; manifest: { assets: Asset[] }; evidence: { publication_eligible: false } };
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
export function parseImages(value: unknown, version: number): Rendition[] {
  if (!object(value) || value.schema_version !== "1.0" || value.article_version !== version ||
      !Array.isArray(value.items) || value.items.length > 20 ||
      !value.items.every(row => object(row) && typeof row.image_id === "string" &&
        /^IMG-[a-f0-9]{32}$/.test(row.image_id) && row.article_version === version &&
        object(row.manifest) && row.manifest.schema_version === "1.0" &&
        Array.isArray(row.manifest.assets) && row.manifest.assets.length > 0 && row.manifest.assets.length <= 3 &&
        row.manifest.assets.every(asset => object(asset) && typeof asset.width === "number" &&
          Number.isInteger(asset.width) && asset.width > 0 && asset.width <= 1920 &&
          typeof asset.height === "number" && Number.isInteger(asset.height) && asset.height > 0 && asset.height <= 1920 &&
          ["jpg", "png", "webp"].includes(String(asset.format)) &&
          ["unknown", "restricted", "verified"].includes(String(asset.license_status)) &&
          typeof asset.ai_generated === "boolean" && typeof asset.alt_text === "string") &&
        object(row.evidence) && row.evidence.publication_eligible === false)) {
    throw new Error("Invalid image records");
  }
  return value.items as Rendition[];
}

function fileData(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = reject;
    reader.onload = () => {
      if (typeof reader.result !== "string" || !reader.result.includes(",")) reject(new Error("invalid file"));
      else resolve(reader.result.slice(reader.result.indexOf(",") + 1));
    };
    reader.readAsDataURL(file);
  });
}

export default function ImageWorkspace({ token, newsId, version, editable }:
  { token: string; newsId: string; version: number; editable: boolean }) {
  const [images, setImages] = useState<Rendition[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [alt, setAlt] = useState("");
  const [source, setSource] = useState("");
  const [rights, setRights] = useState("");
  const [origin, setOrigin] = useState("");
  const [fit, setFit] = useState("contain");
  const [format, setFormat] = useState("webp");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [preview, setPreview] = useState<{ url: string; alt: string } | null>(null);
  const active = useRef<AbortController | null>(null);
  const previewRef = useRef<string | null>(null);
  const path = "/v1/editorial/news/" + encodeURIComponent(newsId) + "/images";
  const query = "?version=" + version;
  function clearPreview() {
    if (previewRef.current) URL.revokeObjectURL(previewRef.current);
    previewRef.current = null; setPreview(null);
  }
  async function load(controller: AbortController) {
    const response = await fetch(path + query, {
      signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
      headers: { Authorization: "Bearer " + token, Accept: "application/json" },
    });
    if (!response.ok) throw new Error(response.status === 403 || response.status === 401 ? "denied" : "unavailable");
    const rows = parseImages(await response.json(), version);
    if (!controller.signal.aborted) setImages(rows);
  }
  useEffect(() => {
    const controller = new AbortController(); active.current = controller;
    setBusy(true);
    load(controller).catch(error => {
      if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "denied"
        ? "مجوز مشاهده تصاویر این خبر وجود ندارد." : "بازیابی تصاویر ناموفق بود.");
    }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => {
      active.current?.abort();
      if (previewRef.current) URL.revokeObjectURL(previewRef.current);
      previewRef.current = null;
    };
  // Each instance is keyed by news and immutable article revision.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, query, token]);

  async function prepare(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editable || busy || !file || file.size === 0 || file.size > 8 * 1024 * 1024 || !origin) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage(""); clearPreview();
    try {
      const encoded = await fileData(file);
      if (controller.signal.aborted) return;
      const response = await fetch(path, {
        method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ article_version: version, image_data: encoded, alt_text: alt,
          source_ref: source, rights_ref: rights.trim() || null, origin,
          profiles: ["landscape", "square", "portrait"], output_format: format, fit }),
      });
      if (response.status === 409) throw new Error("stale");
      if (response.status === 403 || response.status === 401) throw new Error("denied");
      if (!response.ok) throw new Error("unavailable");
      await load(controller);
      if (!controller.signal.aborted) setMessage("نسخه‌های تصویر برای بررسی داخلی ذخیره شدند؛ مجوز انتشار صادر نشده است.");
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "stale"
        ? "نسخه مقاله تغییر کرده است؛ ابتدا آخرین نسخه را بارگیری کنید." :
        error instanceof Error && error.message === "denied" ? "مجوز آماده‌سازی تصویر ندارید." : "آماده‌سازی تصویر ناموفق بود.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function prepareAutomatically() {
    if (!editable || busy) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage(""); clearPreview();
    try {
      const response = await fetch(path + "/auto", {
        method: "POST", signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify({ article_version: version }),
      });
      if (response.status === 409) throw new Error("stale");
      if (response.status === 401 || response.status === 403) throw new Error("denied");
      if (!response.ok) throw new Error("unavailable");
      await load(controller);
      if (!controller.signal.aborted) setMessage("سه کارت خبری از عنوان نسخه ذخیره شد؛ این خروجی عکس واقعی رویداد نیست.");
    } catch (error) {
      if (!controller.signal.aborted) setMessage(error instanceof Error && error.message === "stale"
        ? "ابتدا آخرین نسخه مقاله را بارگیری کنید." :
        error instanceof Error && error.message === "denied" ? "مجوز آماده‌سازی تصویر ندارید." : "ساخت کارت خبری در دسترس نیست.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  }
  async function show(row: Rendition, index: number, asset: Asset) {
    if (busy) return;
    const controller = new AbortController(); active.current = controller;
    setBusy(true); setMessage(""); clearPreview();
    try {
      const response = await fetch(path + "/" + encodeURIComponent(row.image_id) + "/" + index + query,
        { signal: controller.signal, credentials: "same-origin", cache: "no-store", redirect: "error",
          headers: { Authorization: "Bearer " + token } });
      if (!response.ok || !["image/jpeg", "image/png", "image/webp"].includes(response.headers.get("Content-Type") ?? "")) {
        throw new Error("invalid image response");
      }
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob); previewRef.current = url; setPreview({ url, alt: asset.alt_text });
    } catch { if (!controller.signal.aborted) setMessage("نمایش تصویر مجاز نیست یا فایل در دسترس نیست."); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }

  return <section className="news-item grid gap-3" aria-label="آماده‌سازی تصویر">
    <h3>نگار · تصویر نسخه {version.toLocaleString("fa-IR")}</h3>
    <p>خروجی‌های افقی، مربعی و عمودی؛ بدون لوگوی فرضی و بدون مجوز انتشار خودکار.</p>
    {editable && <button type="button" disabled={busy} onClick={() => void prepareAutomatically()}>
      ساخت خودکار کارت خبری از عنوان (بدون آپلود)
    </button>}
    {editable && <form className="grid gap-2" onSubmit={prepare}>
      <fieldset disabled={busy} className="grid gap-2">
        <label>تصویر (JPEG، PNG یا WebP تا ۸ مگابایت)
          <input required type="file" accept="image/jpeg,image/png,image/webp" onChange={event => {
            const next = event.target.files?.[0] ?? null;
            setFile(next && next.size <= 8 * 1024 * 1024 ? next : null);
            if (next && next.size > 8 * 1024 * 1024) setMessage("اندازه فایل بیش از حد مجاز است.");
          }} />
        </label>
        <label>شرح تصویر <input required maxLength={1000} value={alt} onChange={e => setAlt(e.target.value)} /></label>
        <label>مرجع منبع تصویر <input required maxLength={1024} value={source} onChange={e => setSource(e.target.value)} /></label>
        <label>مرجع مدرک حقوق استفاده (در صورت وجود) <input maxLength={1024} value={rights} onChange={e => setRights(e.target.value)} /></label>
        <label>منشأ اعلام‌شده <select required value={origin} onChange={e => setOrigin(e.target.value)}>
          <option value="">انتخاب کنید</option><option value="ai_declared">تولیدشده با هوش مصنوعی</option>
          <option value="non_ai_declared">غیرهوش مصنوعی (ادعای منبع، تأیید نشده)</option>
        </select></label>
        <label>تطبیق ابعاد <select value={fit} onChange={e => setFit(e.target.value)}>
          <option value="contain">حفظ کل تصویر</option><option value="crop">برش مرکزی</option>
        </select></label>
        <label>قالب <select value={format} onChange={e => setFormat(e.target.value)}>
          <option value="webp">WebP</option><option value="jpg">JPEG</option><option value="png">PNG</option>
        </select></label>
        <button type="submit" disabled={!file || !origin || !alt.trim() || !source.trim()}>آماده‌سازی و ذخیره داخلی</button>
      </fieldset>
    </form>}
    {busy && <p role="status">در حال پردازش تصویر…</p>}{message && <p role="status">{message}</p>}
    {images.map(row => <div key={row.image_id} className="grid gap-1">
      {row.manifest.assets.map((asset, index) => <button key={index} type="button" disabled={busy}
        onClick={() => void show(row, index, asset)}>
        {asset.width} × {asset.height} · {asset.format.toUpperCase()} · {asset.ai_generated ? "هوش مصنوعی (اعلام‌شده)" : "منشأ تأیید نشده"}
      </button>)}
      <p>فقط پیش‌نمایش داخلی؛ حقوق انتشار مستقل از آماده‌سازی است.</p>
    </div>)}
    {preview && <img src={preview.url} alt={preview.alt} className="max-w-full max-h-96 object-contain" />}
  </section>;
}
