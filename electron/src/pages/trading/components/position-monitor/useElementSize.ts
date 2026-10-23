/**
 * 容器尺寸测量（ResizeObserver）：虚拟列表需要确定的像素高宽。
 *
 * 首帧用 fallback（jsdom 里 clientWidth/Height 恒为 0，测试需要可渲染的尺寸）；
 * 测量到非零尺寸后不再回落到 fallback，避免切页/隐藏时列表高度跳成 0。
 */
import { useEffect, useRef, useState } from 'react';

export interface ElementSize {
  width: number;
  height: number;
}

export function useElementSize<T extends HTMLElement = HTMLDivElement>(
  fallback: ElementSize,
): { ref: React.RefObject<T | null>; size: ElementSize } {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState<ElementSize>(fallback);
  const lastSizeRef = useRef<ElementSize>(fallback);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const width = Math.round(el.clientWidth);
      const height = Math.round(el.clientHeight);
      if (width <= 0 || height <= 0) return;
      const last = lastSizeRef.current;
      if (last.width === width && last.height === height) return;
      lastSizeRef.current = { width, height };
      setSize({ width, height });
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return { ref, size };
}
