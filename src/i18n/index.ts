import { useContext } from "react";
import { I18nContext } from "./I18nContext";
import T from "./translations";

export function useTranslation() {
  const { lang, setLang } = useContext(I18nContext);
  const t = (key: string, vars?: Record<string, string | number>) => {
    const entry = T[key];
    let text = (entry ? entry[lang] : undefined) || entry?.zh || key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        text = text.replace(`{${k}}`, String(v));
      }
    }
    return text;
  };
  return { t, lang, setLanguage: setLang };
}

export { I18nProvider } from "./I18nContext";
