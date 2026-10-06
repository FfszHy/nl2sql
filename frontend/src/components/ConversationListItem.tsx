import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';

type ConversationListItemProps = {
  id: string;
  title: string;
  active: boolean;
  onSelect: () => void;
  onDelete: () => void;
};

export function ConversationListItem({ id, title, active, onSelect, onDelete }: ConversationListItemProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState({ top: 0, left: 0 });
  const menuId = useId();
  const rowRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const deleteRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const openMenu = () => {
    const bounds = triggerRef.current?.getBoundingClientRect();
    if (!bounds) return;
    setMenuPosition({
      left: Math.max(8, Math.min(bounds.right - 144, window.innerWidth - 152)),
      top: bounds.bottom + 52 <= window.innerHeight - 8 ? bounds.bottom + 4 : Math.max(8, bounds.top - 52)
    });
    setMenuOpen(true);
  };

  useEffect(() => {
    if (!menuOpen) return;
    deleteRef.current?.focus();
    const closeOutside = (event: Event) => {
      if (event.target instanceof Node && !rowRef.current?.contains(event.target) && !menuRef.current?.contains(event.target)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('pointerdown', closeOutside);
    document.addEventListener('focusin', closeOutside);
    const closeOnMove = () => setMenuOpen(false);
    document.addEventListener('scroll', closeOnMove, true);
    window.addEventListener('resize', closeOnMove);
    return () => {
      document.removeEventListener('pointerdown', closeOutside);
      document.removeEventListener('focusin', closeOutside);
      document.removeEventListener('scroll', closeOnMove, true);
      window.removeEventListener('resize', closeOnMove);
    };
  }, [menuOpen]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && menuOpen) {
      event.preventDefault();
      event.stopPropagation();
      setMenuOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === 'Tab' && menuOpen) {
      // Resume the sidebar's normal tab order rather than the portal's position.
      setMenuOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      // This menu has one action; either arrow moves to it.
      if (event.target === triggerRef.current || menuOpen) {
        event.preventDefault();
        event.stopPropagation();
        if (!menuOpen) openMenu();
        deleteRef.current?.focus();
      }
    }
  };

  return (
    <div
      ref={rowRef}
      data-conversation-id={id}
      onKeyDown={handleKeyDown}
      className={`relative flex items-center rounded-md text-sm transition ${
        active ? 'bg-gray-300 text-gray-900' : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
      }`}
    >
      <button
        type="button"
        data-conversation-select={id}
        onClick={() => { setMenuOpen(false); onSelect(); }}
        className="min-w-0 flex-1 truncate rounded-md p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
        title={title}
        aria-current={active ? 'page' : undefined}
      >
        {title}
      </button>
      <button
        ref={triggerRef}
        type="button"
        aria-label={`对话“${title}”的更多操作`}
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        aria-controls={menuOpen ? menuId : undefined}
        onClick={() => menuOpen ? setMenuOpen(false) : openMenu()}
        className="mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-gray-600 hover:bg-gray-400/30 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
      >
        <svg aria-hidden="true" className="h-4 w-4" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="5" cy="12" r="1.8" />
          <circle cx="12" cy="12" r="1.8" />
          <circle cx="19" cy="12" r="1.8" />
        </svg>
      </button>
      {menuOpen && createPortal(
        <div ref={menuRef} id={menuId} role="menu" aria-label={`对话“${title}”的操作`} onKeyDown={handleKeyDown} style={menuPosition} className="fixed z-50 w-36 rounded-md border border-gray-200 bg-white p-1 shadow-md">
          <button
            ref={deleteRef}
            type="button"
            role="menuitem"
            onClick={() => { setMenuOpen(false); onDelete(); }}
            className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 focus:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500"
          >
            <svg aria-hidden="true" className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M3 6h18M9 6V4h6v2M5 6l1 14h12l1-14M10 10v6m4-6v6" />
            </svg>
            删除对话
          </button>
        </div>, document.body
      )}
    </div>
  );
}
