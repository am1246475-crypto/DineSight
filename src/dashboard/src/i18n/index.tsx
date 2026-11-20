import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from "react";
import { usePersisted } from "../hooks/usePersisted";
import { strings, type Lang, type StringKey } from "./strings";

export type { Lang, StringKey };
export type TFn = (key: StringKey, vars?: Record<string, string | number>) => string;

interface I18n {
  lang: Lang;
  dir: "ltr" | "rtl";
  /** BCP-47 tag for dates; Arabic keeps Western digits (0-9) so numbers match the charts */
  locale: string;
  setLang: (l: Lang) => void;
  toggle: () => void;
  t: TFn;
}

const Ctx = createContext<I18n | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLang] = usePersisted<Lang>("dinesight.lang", "en");
  const dir = lang === "ar" ? "rtl" : "ltr";
  const locale = lang === "ar" ? "ar-EG-u-nu-latn" : "en-GB";

  useEffect(() => {
    const root = document.documentElement;
    root.lang = lang;
    root.dir = dir;
  }, [lang, dir]);

  const t = useCallback<TFn>((key, vars) => {
    const text = strings[lang][key] ?? strings.en[key] ?? key;
    return vars ? text.replace(/\{(\w+)\}/g, (_, k: string) => String(vars[k] ?? `{${k}}`)) : text;
  }, [lang]);

  const value = useMemo<I18n>(() => ({ lang, dir, locale, setLang, toggle: () => setLang(lang === "ar" ? "en" : "ar"), t }),
    [lang, dir, locale, setLang, t]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useI18n(): I18n {
  const v = useContext(Ctx);
  if (!v) throw new Error("useI18n must be used inside <I18nProvider>");
  return v;
}
