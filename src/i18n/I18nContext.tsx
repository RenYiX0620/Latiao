import { createContext, useState, useCallback, useMemo, type ReactNode } from "react";
import type { Lang } from "./translations";

export const I18nContext = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({
  lang: "zh",
  setLang: () => {},
});

function getInitialLang(): Lang {
  try {
    return (localStorage.getItem("latiao_language") as Lang) || "zh";
  } catch {
    return "zh";
  }
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(getInitialLang);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    localStorage.setItem("latiao_language", l);
  }, []);

  const value = useMemo(() => ({ lang, setLang }), [lang, setLang]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}
