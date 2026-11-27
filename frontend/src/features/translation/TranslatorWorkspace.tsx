type Props = {
  sourceLanguage: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
};

export default function TranslatorWorkspace({ sourceLanguage, value, onChange, disabled }: Props) {
  return (
    <fieldset className="grid gap-2" disabled={disabled}>
      <legend>مترجم مستقل · ترجمه دستی</legend>
      <p>زبان منبع: <span dir="ltr">{sourceLanguage}</span>؛ زبان مقصد: فارسی. مدل خودکار هنوز متصل نیست.</p>
      <label className="grid gap-2">متن ترجمه فارسی
        <textarea className="border rounded p-2 min-h-28" maxLength={60000}
          value={value} onChange={event => onChange(event.target.value)} />
      </label>
      <p>ترجمه با نسخه خبر ذخیره می‌شود؛ تأیید معنایی و مجوز انتشار مستقل‌اند.</p>
    </fieldset>
  );
}
