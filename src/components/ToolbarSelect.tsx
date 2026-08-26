import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";

export interface ToolbarSelectOption {
  value: string;
  label: string;
  icon: ReactNode;
}

/** 输入工具栏的下拉选择器：lucide 图标 + 向上弹出的菜单（原生 select 无法内嵌 SVG 图标） */
export default function ToolbarSelect({ value, options, onChange, title }: {
  value: string;
  options: ToolbarSelectOption[];
  onChange: (v: string) => void;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const cur = options.find((o) => o.value === value) || options[0];
  return (
    <div className="toolbar-select" ref={ref}>
      <button type="button" className="toolbar-select-btn" title={title} onClick={() => setOpen((o) => !o)}>
        <span className="toolbar-select-icon">{cur?.icon}</span>
        <span>{cur?.label}</span>
        <ChevronDown size={12} className={`toolbar-select-caret${open ? " open" : ""}`} />
      </button>
      {open && (
        <div className="toolbar-select-menu">
          {options.map((o) => (
            <button type="button" key={o.value}
              className={`toolbar-select-option${o.value === value ? " active" : ""}`}
              onClick={() => { onChange(o.value); setOpen(false); }}>
              <span className="toolbar-select-icon">{o.icon}</span>
              <span>{o.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
