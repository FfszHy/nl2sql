import { useState, useRef, useEffect } from 'react';
import ReactECharts from 'echarts-for-react';
import { buildChartOption } from './chart';
import { ConversationListItem } from './components/ConversationListItem';

type Message = {
  role: 'user' | 'assistant';
  content: string;
  sql?: string;
  rows?: any[][];
  columns?: string[];
  explanation?: string;
  retrieval?: {
    enabled?: boolean;
    selected_tables?: string[];
    scores?: Array<{ table_name: string; score: number; raw_score?: number }>;
    fallback_mode?: string;
    top_k?: number;
    score_threshold?: number;
  };
  chartConfig?: unknown;
  chartError?: string;
  echartsCode?: string;
  error?: string;
  errorCode?: number;
  httpStatus?: number;
  isLoading?: boolean;
};

class ApiRequestError extends Error {
  readonly errorType: string;
  readonly code?: number;
  readonly httpStatus?: number;

  constructor(
    message: string,
    errorType: string,
    code?: number,
    httpStatus?: number
  ) {
    super(message);
    this.name = 'ApiRequestError';
    this.errorType = errorType;
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

type RegisteredDataSource = {
  id: string;
  name: string;
  host: string;
  database: string;
};

type SemanticColumnConfig = {
  column_name: string;
  required: boolean;
  configured: boolean;
  field_comment: string;
  field_aliases: string[];
  field_type: string;
};

type SemanticTableConfig = {
  table_name: string;
  table_comment: string;
  table_comment_configured?: boolean;
  columns: SemanticColumnConfig[];
};

type SemanticIndexStatus = {
  vectorized: boolean;
  indexed_tables: number;
  updated_at: string | null;
};

type SemanticBootstrapResult = {
  auto_configured?: boolean;
  saved_table_count?: number;
  saved_field_count?: number;
  vectorized?: boolean;
  indexed_tables?: number;
  updated_at?: string | null;
  warning?: string | null;
};

type Conversation = {
  id: string;
  title: string;
  messages: Message[];
  updatedAt: number;
};

const CONVERSATIONS_STORAGE_KEY = 'chat_conversations';
const ACTIVE_CONVERSATION_STORAGE_KEY = 'active_conversation_id';

const createConversationId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `conv_${Date.now()}_${Math.random().toString(16).slice(2)}`;
};

const createDefaultConversation = (): Conversation => ({
  id: createConversationId(),
  title: '新对话',
  messages: [],
  updatedAt: Date.now()
});

const loadConversationsFromStorage = (): Conversation[] => {
  const raw = localStorage.getItem(CONVERSATIONS_STORAGE_KEY);
  if (!raw) {
    return [createDefaultConversation()];
  }

  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [createDefaultConversation()];
    }
    const normalized = parsed
      .map((item) => {
        if (!item || typeof item !== 'object') return null;
        if (typeof item.id !== 'string') return null;
        const title = typeof item.title === 'string' && item.title.trim() ? item.title : '新对话';
        const messages = Array.isArray(item.messages) ? item.messages : [];
        const updatedAt = typeof item.updatedAt === 'number' ? item.updatedAt : Date.now();
        return { id: item.id, title, messages, updatedAt } as Conversation;
      })
      .filter((item): item is Conversation => Boolean(item));

    return normalized.length > 0 ? normalized : [createDefaultConversation()];
  } catch {
    return [createDefaultConversation()];
  }
};

const buildConversationTitle = (question: string) => {
  const text = question.trim().replace(/\s+/g, ' ');
  if (!text) return '新对话';
  return text.length > 18 ? `${text.slice(0, 18)}...` : text;
};

function App() {
  const [conversations, setConversations] = useState<Conversation[]>(() => loadConversationsFromStorage());
  const [activeConversationId, setActiveConversationId] = useState(() => {
    const stored = localStorage.getItem(ACTIVE_CONVERSATION_STORAGE_KEY) || '';
    const existing = loadConversationsFromStorage();
    if (stored && existing.find((item) => item.id === stored)) return stored;
    return existing[0]?.id || '';
  });
  const [input, setInput] = useState('');
  const [dataSourceId, setDataSourceId] = useState(() => localStorage.getItem('data_source_id') || '');
  const [showSettings, setShowSettings] = useState(false);
  
  // 新增：后端服务地址和数据库配置状态
  const [backendUrl, setBackendUrl] = useState(() => localStorage.getItem('backend_url') || '');
  const [dbHost, setDbHost] = useState(() => localStorage.getItem('db_host') || '127.0.0.1');
  const [dbPort, setDbPort] = useState(() => localStorage.getItem('db_port') || '5432');
  const [dbUser, setDbUser] = useState(() => localStorage.getItem('db_user') || 'postgres');
  const [dbPassword, setDbPassword] = useState(() => localStorage.getItem('db_password') || '');
  const [dbName, setDbName] = useState(() => localStorage.getItem('db_name') || '');
  const [isRegistering, setIsRegistering] = useState(false);
  const [includeChart, setIncludeChart] = useState(() => localStorage.getItem('include_chart') === 'true');
  const [showSemanticModal, setShowSemanticModal] = useState(false);
  const [semanticTables, setSemanticTables] = useState<SemanticTableConfig[]>([]);
  const [selectedSemanticTable, setSelectedSemanticTable] = useState('');
  const [editingSemanticCell, setEditingSemanticCell] = useState<{
    table_name: string;
    column_name: string;
    field: 'field_comment' | 'field_aliases' | 'field_type';
  } | null>(null);
  const [semanticStatus, setSemanticStatus] = useState('');
  const [isSemanticLoading, setIsSemanticLoading] = useState(false);
  const [isSemanticSaving, setIsSemanticSaving] = useState(false);
  const [isSemanticVectorizing, setIsSemanticVectorizing] = useState(false);
  const [isSemanticIndexStatusLoading, setIsSemanticIndexStatusLoading] = useState(false);
  const [semanticIndexStatus, setSemanticIndexStatus] = useState<SemanticIndexStatus | null>(null);
  const [registeredDataSources, setRegisteredDataSources] = useState<RegisteredDataSource[]>(() => {
    const raw = localStorage.getItem('registered_data_sources');
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const conversationListRef = useRef<HTMLDivElement>(null);
  const pendingConversationFocusRef = useRef(false);
  const activeConversation = conversations.find((item) => item.id === activeConversationId) || null;
  const messages = activeConversation?.messages || [];
  const selectedSemanticTableDetail =
    semanticTables.find((item) => item.table_name === selectedSemanticTable) || null;

  useEffect(() => {
    if (!conversations.length) {
      const fallback = createDefaultConversation();
      setConversations([fallback]);
      setActiveConversationId(fallback.id);
      return;
    }
    if (!activeConversationId || !conversations.some((item) => item.id === activeConversationId)) {
      setActiveConversationId(conversations[0].id);
    }
  }, [conversations, activeConversationId]);

  useEffect(() => {
    localStorage.setItem(CONVERSATIONS_STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations]);

  useEffect(() => {
    if (!pendingConversationFocusRef.current) return;
    const buttons = conversationListRef.current?.querySelectorAll<HTMLButtonElement>('button[data-conversation-select]');
    const target = Array.from(buttons || []).find((button) => button.getAttribute('data-conversation-select') === activeConversationId);
    if (!target) return;
    target.focus();
    pendingConversationFocusRef.current = false;
  }, [conversations, activeConversationId]);

  useEffect(() => {
    if (activeConversationId) {
      localStorage.setItem(ACTIVE_CONVERSATION_STORAGE_KEY, activeConversationId);
    }
  }, [activeConversationId]);

  const updateActiveConversationMessages = (
    updater: (prevMessages: Message[]) => Message[],
    preferredTitle?: string
  ) => {
    if (!activeConversationId) return;

    setConversations((prev) =>
      prev
        .map((item) => {
          if (item.id !== activeConversationId) return item;
          const nextMessages = updater(item.messages || []);
          const nextTitle =
            preferredTitle ||
            (item.title === '新对话' && nextMessages.length > 0 && nextMessages[0].role === 'user'
              ? buildConversationTitle(nextMessages[0].content)
              : item.title);
          return {
            ...item,
            title: nextTitle,
            messages: nextMessages,
            updatedAt: Date.now()
          };
        })
        .sort((a, b) => b.updatedAt - a.updatedAt)
    );
  };

  const createNewConversation = () => {
    const newConversation = createDefaultConversation();
    setConversations((prev) => [newConversation, ...prev]);
    setActiveConversationId(newConversation.id);
    setInput('');
  };

  const deleteConversation = (conversationId: string) => {
    if (!conversations.some((item) => item.id === conversationId)) return;
    // Create once in the event, then derive removal from the latest queued state.
    const fallback = createDefaultConversation();
    pendingConversationFocusRef.current = true;
    setConversations((prev) => {
      const remaining = prev.filter((item) => item.id !== conversationId);
      return remaining.length ? remaining : [fallback];
    });
    if (activeConversationId === conversationId) {
      setInput('');
    }
    // The existing activation effect selects the first remaining/new conversation.
  };

  const normalizeBaseUrl = () => {
    const raw = backendUrl.trim();
    if (!raw) return '';

    const withProtocol = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
    try {
      return new URL(withProtocol).origin;
    } catch {
      throw new Error('后端服务地址格式不正确，请填写如 http://127.0.0.1:8000');
    }
  };

  const requestJson = async (path: string, init: RequestInit) => {
    const baseUrl = normalizeBaseUrl();
    let response: Response;
    let rawText: string;
    try {
      response = await fetch(`${baseUrl}${path}`, init);
      rawText = await response.text();
    } catch {
      throw new ApiRequestError('无法连接后端服务，请检查网络、服务地址和后端运行状态。', 'network_error');
    }
    let data: any = null;
    if (rawText) {
      try {
        data = JSON.parse(rawText);
      } catch {
        if (!response.ok) {
          throw new ApiRequestError(`HTTP ${response.status}: ${rawText.slice(0, 200)}`, 'http_error', undefined, response.status);
        }
        throw new ApiRequestError('接口返回了非 JSON 内容，请检查后端服务地址或代理配置。', 'response_error', undefined, response.status);
      }
    }

    if (!response.ok) {
      const errorMessage =
        data?.message ||
        `HTTP ${response.status}${rawText ? `: ${rawText.slice(0, 200)}` : ''}`;
      throw new ApiRequestError(errorMessage, data?.error_type || 'http_error', data?.code, response.status);
    }

    if (!data) {
      throw new ApiRequestError(
        '接口返回为空，通常是请求地址不对。请检查“后端服务地址”是否正确，或在开发环境中补齐 Vite 代理。',
        'response_error',
        undefined,
        response.status
      );
    }

    return data;
  };

  const flattenSemanticPayload = (tables: SemanticTableConfig[]) => {
    const tableConfigs: Array<{
      table_name: string;
      table_comment: string;
    }> = [];
    const fields: Array<{
      table_name: string;
      column_name: string;
      field_comment: string;
      field_aliases: string[];
      field_type: string;
    }> = [];
    tables.forEach((table) => {
      tableConfigs.push({
        table_name: table.table_name,
        table_comment: (table.table_comment || '').trim()
      });
      table.columns.forEach((column) => {
        fields.push({
          table_name: table.table_name,
          column_name: column.column_name,
          field_comment: (column.field_comment || '').trim(),
          field_aliases: (column.field_aliases || []).map((item) => item.trim()).filter(Boolean),
          field_type: (column.field_type || '').trim()
        });
      });
    });
    return { tables: tableConfigs, fields };
  };

  const loadSemanticIndexStatus = async (targetDataSourceId?: string) => {
    const currentDataSourceId = (targetDataSourceId ?? dataSourceId).trim();
    if (!currentDataSourceId) {
      setSemanticIndexStatus(null);
      return;
    }
    setIsSemanticIndexStatusLoading(true);
    try {
      const data = await requestJson(`/datasources/${currentDataSourceId}/semantic-index/status`, {
        method: 'GET'
      });
      if (data.code === 0 && data.data) {
        setSemanticIndexStatus({
          vectorized: Boolean(data.data.vectorized),
          indexed_tables: Number(data.data.indexed_tables || 0),
          updated_at: data.data.updated_at || null
        });
      } else {
        setSemanticIndexStatus(null);
      }
    } catch {
      setSemanticIndexStatus(null);
    } finally {
      setIsSemanticIndexStatusLoading(false);
    }
  };

  const loadSemanticConfigSchema = async () => {
    if (!dataSourceId.trim()) {
      alert('请先选择数据源后再配置语义字段');
      return;
    }
    setIsSemanticLoading(true);
    setSemanticStatus('');
    try {
      const data = await requestJson(`/datasources/${dataSourceId.trim()}/semantic-config/schema`, {
        method: 'GET'
      });
      if (data.code !== 0) {
        setSemanticStatus(data.message || '加载语义配置失败');
        return;
      }
      const tables = Array.isArray(data?.data?.tables) ? data.data.tables : [];
      setSemanticTables(tables);
      if (tables.length > 0) {
        const hasCurrent = tables.some((item: SemanticTableConfig) => item.table_name === selectedSemanticTable);
        setSelectedSemanticTable(hasCurrent ? selectedSemanticTable : tables[0].table_name);
      } else {
        setSelectedSemanticTable('');
      }
      setSemanticStatus(
        data?.data?.all_configured
          ? '当前数据源表/字段语义配置已完整，可直接向量化。'
          : `仍有 ${data?.data?.missing_table_count || 0} 张表注释、${data?.data?.missing_field_count || 0} 个字段待配置。`
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSemanticStatus(`加载语义配置失败: ${message}`);
    } finally {
      setIsSemanticLoading(false);
    }
  };

  const openSemanticConfigPanel = async () => {
    setShowSemanticModal(true);
    setEditingSemanticCell(null);
    await loadSemanticConfigSchema();
  };

  const updateSemanticField = (
    tableName: string,
    columnName: string,
    patch: Partial<Pick<SemanticColumnConfig, 'field_comment' | 'field_aliases' | 'field_type'>>
  ) => {
    setSemanticTables((prev) =>
      prev.map((table) => {
        if (table.table_name !== tableName) return table;
        return {
          ...table,
          columns: table.columns.map((column) => {
            if (column.column_name !== columnName) return column;
            return {
              ...column,
              ...patch
            };
          })
        };
      })
    );
  };

  const updateSemanticTable = (
    tableName: string,
    patch: Partial<Pick<SemanticTableConfig, 'table_comment'>>
  ) => {
    setSemanticTables((prev) =>
      prev.map((table) => {
        if (table.table_name !== tableName) return table;
        return {
          ...table,
          ...patch
        };
      })
    );
  };

  const saveSemanticConfig = async () => {
    if (!dataSourceId.trim()) {
      alert('请先选择数据源');
      return;
    }
    const payload = flattenSemanticPayload(semanticTables);
    setIsSemanticSaving(true);
    setSemanticStatus('');
    try {
      const data = await requestJson(`/datasources/${dataSourceId.trim()}/semantic-config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (data.code === 0) {
        const savedTableCount = data?.data?.saved_table_count || 0;
        const savedFieldCount = data?.data?.saved_field_count || 0;
        const reindexed = Boolean(data?.data?.reindexed);
        const indexedTables = Number(data?.data?.indexed_tables || 0);
        const reindexError = data?.data?.reindex_error;
        alert(
          reindexed
            ? `语义配置已保存并自动重建向量（表数: ${savedTableCount}，字段数: ${savedFieldCount}，索引表: ${indexedTables}）`
            : `语义配置已保存，但自动重建向量失败：${reindexError || '未知错误'}`
        );
        await loadSemanticIndexStatus();
        setShowSemanticModal(false);
        setSemanticStatus('');
      } else {
        setSemanticStatus(data.message || '保存失败');
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSemanticStatus(`保存失败: ${message}`);
    } finally {
      setIsSemanticSaving(false);
    }
  };

  const runSemanticVectorization = async () => {
    if (!dataSourceId.trim()) {
      alert('请先选择数据源');
      return;
    }
    setIsSemanticVectorizing(true);
    setSemanticStatus('');
    try {
      const data = await requestJson(`/datasources/${dataSourceId.trim()}/semantic-index/refresh`, {
        method: 'POST'
      });
      if (data.code === 0) {
        setSemanticStatus(`向量化完成，索引表数量: ${data?.data?.indexed_tables || 0}`);
      } else {
        setSemanticStatus(data.message || '向量化失败');
      }
      await loadSemanticConfigSchema();
      await loadSemanticIndexStatus();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setSemanticStatus(`向量化失败: ${message}`);
    } finally {
      setIsSemanticVectorizing(false);
    }
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (!showSettings) return;
    if (!dataSourceId.trim()) {
      setSemanticIndexStatus(null);
      return;
    }
    loadSemanticIndexStatus();
  }, [showSettings, dataSourceId]);

  const deleteSelectedDataSource = () => {
    if (!dataSourceId) {
      alert('请先选择要删除的数据源');
      return;
    }

    const next = registeredDataSources.filter((item) => item.id !== dataSourceId);
    setRegisteredDataSources(next);
    localStorage.setItem('registered_data_sources', JSON.stringify(next));
    setDataSourceId('');
    localStorage.removeItem('data_source_id');
  };

  const saveSettings = async () => {
    localStorage.setItem('backend_url', backendUrl);
    localStorage.setItem('db_host', dbHost);
    localStorage.setItem('db_port', dbPort);
    localStorage.setItem('db_user', dbUser);
    localStorage.setItem('db_password', dbPassword);
    localStorage.setItem('db_name', dbName);

    if (!dbHost || !dbPort || !dbUser || !dbName) {
      alert("请填写完整的数据库配置信息");
      return;
    }
    if (!/^\d+$/.test(dbPort.trim())) {
      alert("数据库端口必须是数字");
      return;
    }

    setIsRegistering(true);
    try {
      const data = await requestJson('/datasources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: `Auto Register ${dbName}`,
          datasource: {
            host: dbHost,
            port: Number(dbPort),
            user: dbUser,
            password: dbPassword,
            database: dbName
          }
        })
      });
      if (data.code === 0 && data.data && data.data.data_source_id) {
        const newId = data.data.data_source_id;
        const bootstrap: SemanticBootstrapResult = data?.data?.semantic_bootstrap || {};
        const newDataSource: RegisteredDataSource = {
          id: newId,
          name: data.data.name || `Auto Register ${dbName}`,
          host: dbHost,
          database: dbName
        };
        setRegisteredDataSources((prev) => {
          const next = [newDataSource, ...prev.filter((item) => item.id !== newId)];
          localStorage.setItem('registered_data_sources', JSON.stringify(next));
          return next;
        });
        setDataSourceId(newId);
        localStorage.setItem('data_source_id', newId);
        setDbHost('');
        setDbPort('');
        setDbUser('');
        setDbPassword('');
        setDbName('');
        localStorage.setItem('db_host', '');
        localStorage.setItem('db_port', '');
        localStorage.setItem('db_user', '');
        localStorage.setItem('db_password', '');
        localStorage.setItem('db_name', '');
        const bootstrapMessage = bootstrap.auto_configured
          ? `已自动初始化语义并向量化（索引表 ${bootstrap.indexed_tables || 0}）`
          : `自动初始化未完成：${bootstrap.warning || '未知错误，可在设置中手动重试向量化'}`;
        alert(`数据源注册成功！ID: ${newId}\n${bootstrapMessage}`);
        setShowSettings(false);
      } else {
        alert(`注册失败: ${data.message || '未知错误'}`);
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      alert(`注册请求失败: ${errorMessage}`);
    } finally {
      setIsRegistering(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;
    if (!dataSourceId.trim()) {
      alert("请先打开设置面板配置并注册数据源");
      setShowSettings(true);
      return;
    }

    const userMessage = input.trim();
    setInput('');
    updateActiveConversationMessages(
      (prev) => [...prev, { role: 'user', content: userMessage }, { role: 'assistant', content: '', isLoading: true }],
      buildConversationTitle(userMessage)
    );

    try {
      const data = await requestJson('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          question: userMessage,
          data_source_id: dataSourceId.trim(),
          options: {
            max_rows: 200,
            retry_on_error: true,
            include_explanation: true,
            include_chart: includeChart
          }
        })
      });
      
      updateActiveConversationMessages((prev) => {
        const newMessages = [...prev];
        const lastIndex = newMessages.length - 1;
        if (lastIndex < 0) return prev;
        
        if (data.code === 0) {
          newMessages[lastIndex] = {
            role: 'assistant',
            content: data.data.response || '查询成功',
            sql: data.data.sql,
            rows: data.data.rows,
            columns: data.data.columns,
            explanation: data.data.explanation,
            retrieval: data.data.retrieval,
            chartConfig: data.data.chart_config,
            chartError: data.data.chart_error,
            isLoading: false
          };
        } else {
          newMessages[lastIndex] = {
            role: 'assistant',
            content: data.message || '查询失败',
            error: data.error_type || 'query_error',
            errorCode: data.code,
            isLoading: false
          };
        }
        return newMessages;
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      const requestError = error instanceof ApiRequestError ? error : null;
      updateActiveConversationMessages((prev) => {
        const newMessages = [...prev];
        if (!newMessages.length) return prev;
        newMessages[newMessages.length - 1] = {
          role: 'assistant',
          content: errorMessage || '查询失败，请稍后重试。',
          error: requestError?.errorType || 'request_error',
          errorCode: requestError?.code,
          httpStatus: requestError?.httpStatus,
          isLoading: false
        };
        return newMessages;
      });
    }
  };

  const settingsPanel = (
    <div
      className="absolute inset-0 z-20 bg-black/30 flex items-center justify-center p-4 md:p-8"
      onClick={() => setShowSettings(false)}
    >
      <div
        className="w-full max-w-5xl max-h-[88vh] bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b flex items-center justify-between">
          <div className="text-base font-semibold text-gray-800">服务与数据源设置</div>
          <button
            type="button"
            onClick={() => setShowSettings(false)}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            关闭
          </button>
        </div>

        <div className="p-5 overflow-y-auto max-h-[calc(88vh-64px)] space-y-5">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-3">
              <div>
                <label className="block text-gray-600 mb-1 text-sm">后端服务地址</label>
                <input
                  type="text"
                  value={backendUrl}
                  onChange={e => setBackendUrl(e.target.value)}
                  className="w-full border rounded-md p-2 outline-none focus:border-blue-400"
                  placeholder="例如: http://localhost:8000"
                />
              </div>

              <div className="pt-3 border-t border-gray-200">
                <label className="block text-gray-700 font-medium mb-2 text-sm">数据库配置（PostgreSQL / Supabase）</label>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  <div>
                    <label className="block text-gray-600 mb-1 text-xs">Host</label>
                    <input
                      type="text"
                      value={dbHost}
                      onChange={e => setDbHost(e.target.value)}
                      className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm"
                      placeholder="127.0.0.1"
                    />
                  </div>
                  <div>
                    <label className="block text-gray-600 mb-1 text-xs">Port</label>
                    <input
                      type="text"
                      value={dbPort}
                      onChange={e => setDbPort(e.target.value)}
                      className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm"
                      placeholder="5432"
                    />
                  </div>
                  <div>
                    <label className="block text-gray-600 mb-1 text-xs">User</label>
                    <input
                      type="text"
                      value={dbUser}
                      onChange={e => setDbUser(e.target.value)}
                      className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm"
                      placeholder="postgres"
                    />
                  </div>
                  <div>
                    <label className="block text-gray-600 mb-1 text-xs">Password</label>
                    <input
                      type="password"
                      value={dbPassword}
                      onChange={e => setDbPassword(e.target.value)}
                      className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm"
                      placeholder="password"
                    />
                  </div>
                  <div className="sm:col-span-2">
                    <label className="block text-gray-600 mb-1 text-xs">Database</label>
                    <input
                      type="text"
                      value={dbName}
                      onChange={e => setDbName(e.target.value)}
                      className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm"
                      placeholder="database_name"
                    />
                  </div>
                </div>

                <button
                  onClick={saveSettings}
                  disabled={isRegistering}
                  className={`w-full text-white rounded py-2 mt-3 transition ${isRegistering ? 'bg-gray-400 cursor-not-allowed' : 'bg-blue-500 hover:bg-blue-600'}`}
                >
                  {isRegistering ? '注册中...' : '保存并注册数据源'}
                </button>
              </div>
            </div>

            <div className="space-y-3">
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="block text-gray-700 font-medium text-sm">已注册数据源</label>
                  <button
                    type="button"
                    onClick={deleteSelectedDataSource}
                    className="text-xs text-red-500 hover:text-red-600"
                  >
                    删除选中
                  </button>
                </div>
                <select
                  value={dataSourceId}
                  onChange={(e) => {
                    const selectedId = e.target.value;
                    setDataSourceId(selectedId);
                    localStorage.setItem('data_source_id', selectedId);
                    setShowSemanticModal(false);
                    setSemanticTables([]);
                    setSelectedSemanticTable('');
                    setSemanticStatus('');
                  }}
                  className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm bg-white"
                >
                  <option value="">请选择数据源</option>
                  {registeredDataSources.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.database}
                    </option>
                  ))}
                  {dataSourceId && !registeredDataSources.find((item) => item.id === dataSourceId) && (
                    <option value={dataSourceId}>当前使用（历史数据源）</option>
                  )}
                </select>
                <div className="mt-2 flex gap-2">
                  <div className="flex-1 text-xs rounded border px-2 py-2 bg-gray-50 text-gray-600 border-gray-200">
                    {!dataSourceId
                      ? '向量化状态：请选择数据源'
                      : isSemanticIndexStatusLoading
                        ? '向量化状态：加载中...'
                        : semanticIndexStatus?.vectorized
                          ? `向量化状态：已向量化（索引表 ${semanticIndexStatus.indexed_tables}${
                              semanticIndexStatus.updated_at ? `，更新时间 ${semanticIndexStatus.updated_at}` : ''
                            }）`
                          : '向量化状态：未向量化'}
                  </div>
                </div>
                <div className="mt-2 flex gap-2">
                  <button
                    type="button"
                    onClick={openSemanticConfigPanel}
                    disabled={!dataSourceId || isSemanticLoading}
                    className={`flex-1 text-sm rounded py-2 border transition ${
                      !dataSourceId || isSemanticLoading
                        ? 'bg-gray-100 text-gray-400 border-gray-200 cursor-not-allowed'
                        : 'bg-white text-blue-600 border-blue-300 hover:bg-blue-50'
                    }`}
                  >
                    {isSemanticLoading ? '加载中...' : '配置语义'}
                  </button>
                  <button
                    type="button"
                    onClick={runSemanticVectorization}
                    disabled={!dataSourceId || isSemanticVectorizing}
                    className={`flex-1 text-sm rounded py-2 transition ${
                      !dataSourceId || isSemanticVectorizing
                        ? 'bg-gray-300 text-white cursor-not-allowed'
                        : 'bg-indigo-500 text-white hover:bg-indigo-600'
                    }`}
                  >
                    {isSemanticVectorizing
                      ? '向量化中...'
                      : semanticIndexStatus?.vectorized
                        ? '重新向量化'
                        : '向量化'}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );

  const semanticConfigPanel = (
    <div
      className="absolute inset-0 z-30 bg-black/30 flex items-center justify-center p-4 md:p-8"
      onClick={() => setShowSemanticModal(false)}
    >
      <div
        className="w-full max-w-4xl max-h-[88vh] bg-white rounded-2xl shadow-xl border border-gray-200 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b flex items-center justify-between">
          <div className="text-base font-semibold text-gray-800">语义配置</div>
          <button
            type="button"
            onClick={() => setShowSemanticModal(false)}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            关闭
          </button>
        </div>
        <div className="p-5 overflow-y-auto max-h-[calc(88vh-64px)] space-y-3">
          {semanticStatus && (
            <div className="text-xs text-gray-600 bg-gray-50 border rounded p-3 whitespace-pre-wrap">
              {semanticStatus}
            </div>
          )}
          {semanticTables.length > 0 ? (
            <>
              <select
                value={selectedSemanticTable}
                onChange={(e) => setSelectedSemanticTable(e.target.value)}
                className="w-full border rounded p-2 outline-none focus:border-blue-400 text-sm bg-white"
              >
                {semanticTables.map((table) => (
                  <option key={table.table_name} value={table.table_name}>
                    {table.table_name}
                  </option>
                ))}
              </select>
              {selectedSemanticTableDetail && (
                <div className="bg-white border rounded p-3 space-y-3">
                  <div>
                    <label className="block text-xs text-gray-500 mb-1">表注释</label>
                    <input
                      type="text"
                      value={selectedSemanticTableDetail.table_comment || ''}
                      onChange={(e) =>
                        updateSemanticTable(selectedSemanticTableDetail.table_name, {
                          table_comment: e.target.value
                        })
                      }
                      className="w-full border rounded p-1.5 text-sm"
                      placeholder="请填写表注释（必填）"
                    />
                  </div>
                  <div className="max-h-80 overflow-auto border rounded">
                    <table className="w-full text-sm border-collapse">
                      <thead className="bg-gray-50 sticky top-0">
                        <tr>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-600 border-b w-56">
                            字段名
                          </th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-600 border-b">
                            字段注释（双击编辑）
                          </th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-600 border-b">
                            字段别名（逗号分隔，双击编辑）
                          </th>
                          <th className="px-3 py-2 text-left text-xs font-medium text-gray-600 border-b">
                            字段类型（双击编辑）
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedSemanticTableDetail.columns.map((column) => {
                          const isEditingComment =
                            editingSemanticCell?.table_name === selectedSemanticTableDetail.table_name &&
                            editingSemanticCell?.column_name === column.column_name &&
                            editingSemanticCell?.field === 'field_comment';
                          const isEditingAliases =
                            editingSemanticCell?.table_name === selectedSemanticTableDetail.table_name &&
                            editingSemanticCell?.column_name === column.column_name &&
                            editingSemanticCell?.field === 'field_aliases';
                          const isEditingType =
                            editingSemanticCell?.table_name === selectedSemanticTableDetail.table_name &&
                            editingSemanticCell?.column_name === column.column_name &&
                            editingSemanticCell?.field === 'field_type';

                          return (
                            <tr key={column.column_name} className="odd:bg-white even:bg-gray-50/60">
                              <td className="px-3 py-2 border-b text-xs font-medium text-gray-700 align-top">
                                {column.column_name}
                              </td>
                              <td
                                className="px-3 py-2 border-b align-top cursor-text"
                                onDoubleClick={() =>
                                  setEditingSemanticCell({
                                    table_name: selectedSemanticTableDetail.table_name,
                                    column_name: column.column_name,
                                    field: 'field_comment'
                                  })
                                }
                              >
                                {isEditingComment ? (
                                  <input
                                    type="text"
                                    autoFocus
                                    value={column.field_comment || ''}
                                    onChange={(e) =>
                                      updateSemanticField(selectedSemanticTableDetail.table_name, column.column_name, {
                                        field_comment: e.target.value
                                      })
                                    }
                                    onBlur={() => setEditingSemanticCell(null)}
                                    className="w-full border rounded p-1.5 text-sm"
                                    placeholder="字段注释"
                                  />
                                ) : (
                                  <span className="text-gray-800">{column.field_comment || '-'}</span>
                                )}
                              </td>
                              <td
                                className="px-3 py-2 border-b align-top cursor-text"
                                onDoubleClick={() =>
                                  setEditingSemanticCell({
                                    table_name: selectedSemanticTableDetail.table_name,
                                    column_name: column.column_name,
                                    field: 'field_aliases'
                                  })
                                }
                              >
                                {isEditingAliases ? (
                                  <input
                                    type="text"
                                    autoFocus
                                    value={(column.field_aliases || []).join(',')}
                                    onChange={(e) =>
                                      updateSemanticField(selectedSemanticTableDetail.table_name, column.column_name, {
                                        field_aliases: e.target.value
                                          .split(',')
                                          .map((item) => item.trim())
                                          .filter(Boolean)
                                      })
                                    }
                                    onBlur={() => setEditingSemanticCell(null)}
                                    className="w-full border rounded p-1.5 text-sm"
                                    placeholder="字段别名，逗号分隔"
                                  />
                                ) : (
                                  <span className="text-gray-800">
                                    {(column.field_aliases || []).join(', ') || '-'}
                                  </span>
                                )}
                              </td>
                              <td
                                className="px-3 py-2 border-b align-top cursor-text"
                                onDoubleClick={() =>
                                  setEditingSemanticCell({
                                    table_name: selectedSemanticTableDetail.table_name,
                                    column_name: column.column_name,
                                    field: 'field_type'
                                  })
                                }
                              >
                                {isEditingType ? (
                                  <input
                                    type="text"
                                    autoFocus
                                    value={column.field_type || ''}
                                    onChange={(e) =>
                                      updateSemanticField(selectedSemanticTableDetail.table_name, column.column_name, {
                                        field_type: e.target.value
                                      })
                                    }
                                    onBlur={() => setEditingSemanticCell(null)}
                                    className="w-full border rounded p-1.5 text-sm"
                                    placeholder="字段类型"
                                  />
                                ) : (
                                  <span className="text-gray-800">{column.field_type || '-'}</span>
                                )}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
              <button
                type="button"
                onClick={saveSemanticConfig}
                disabled={isSemanticSaving}
                className={`w-full text-sm text-white rounded py-2 transition ${
                  isSemanticSaving ? 'bg-gray-400 cursor-not-allowed' : 'bg-blue-500 hover:bg-blue-600'
                }`}
              >
                {isSemanticSaving ? '保存中...' : '保存语义配置'}
              </button>
            </>
          ) : (
            <div className="text-sm text-gray-500">暂无可配置表，请检查数据源或稍后重试。</div>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div className="flex h-screen bg-white">
      {/* Sidebar */}
      <div className="hidden md:flex w-64 bg-gray-50 border-r flex-col">
        <div className="p-4 flex items-center justify-between">
          <button 
            onClick={createNewConversation}
            className="flex items-center space-x-2 text-sm font-medium text-gray-700 hover:text-gray-900 transition"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
            </svg>
            <span>新对话</span>
          </button>
          <button onClick={() => setShowSettings(!showSettings)} className="text-gray-500 hover:text-gray-700">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </button>
        </div>

        <div ref={conversationListRef} className="flex-1 overflow-y-auto p-4 space-y-2">
          {conversations.map((conversation) => (
            <ConversationListItem
              key={conversation.id}
              id={conversation.id}
              title={conversation.title}
              active={conversation.id === activeConversationId}
              onSelect={() => setActiveConversationId(conversation.id)}
              onDelete={() => deleteConversation(conversation.id)}
            />
          ))}
        </div>
        <div className="p-4 border-t">
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 rounded-full bg-blue-500 flex items-center justify-center text-white font-bold">
              U
            </div>
            <span className="text-sm font-medium text-gray-700">User</span>
          </div>
        </div>
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col relative bg-white">
        {/* Header (Mobile) */}
        <div className="md:hidden flex items-center justify-between p-4 border-b">
          <span className="font-medium text-gray-700">智能问数</span>
          <button onClick={() => setShowSettings(!showSettings)} className="text-gray-500">设置</button>
        </div>

        {showSettings && settingsPanel}
        {showSemanticModal && semanticConfigPanel}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 space-y-6 pb-32">
          {messages.length === 0 ? (
            <div className="h-full flex flex-col items-center justify-center text-center space-y-4">
              <h1 className="text-3xl font-semibold text-gray-800">智能问数</h1>
              <p className="text-gray-500">向我提问关于数据库的问题。</p>
            </div>
          ) : (
            messages.map((msg, index) => (
              <div key={index} className={`flex flex-col space-y-2 ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                <div className={`max-w-4xl w-full flex space-x-3 ${msg.role === 'user' ? 'flex-row-reverse space-x-reverse' : 'flex-row'}`}>
                  {/* Avatar */}
                  <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center font-bold text-white ${msg.role === 'user' ? 'bg-blue-500' : 'bg-green-500'}`}>
                    {msg.role === 'user' ? 'U' : 'AI'}
                  </div>
                  
                  {/* Message Bubble */}
                  <div className={`p-4 rounded-2xl max-w-[90%] ${msg.role === 'user' ? 'bg-gray-100 text-gray-900 rounded-tr-sm' : 'bg-white border text-gray-800 rounded-tl-sm'} shadow-sm`}>
                    {msg.isLoading ? (
                      <div className="flex space-x-2 items-center h-6">
                        <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce"></div>
                        <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }}></div>
                        <div className="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style={{ animationDelay: '0.4s' }}></div>
                      </div>
                    ) : (
                      <div className="space-y-4 text-sm">
                        {msg.error ? (
                          <div role="alert" className="text-red-500 bg-red-50 p-3 rounded-md text-xs border border-red-100">
                            <strong>查询失败</strong>
                            <div className="whitespace-pre-wrap leading-relaxed mt-1">{msg.content}</div>
                            <div className="font-mono mt-2 break-words">
                              {msg.error}
                              {typeof msg.errorCode === 'number' ? ` · 错误码 ${msg.errorCode}` : ''}
                              {typeof msg.httpStatus === 'number' ? ` · HTTP ${msg.httpStatus}` : ''}
                            </div>
                          </div>
                        ) : (
                          <div className="whitespace-pre-wrap leading-relaxed">{msg.content}</div>
                        )}

                        {msg.explanation && (
                          <div className="text-gray-600 italic border-l-4 border-gray-300 pl-3 py-1">
                            {msg.explanation}
                          </div>
                        )}
                        
                        {msg.sql && (
                          <div className="bg-gray-900 text-gray-100 p-4 rounded-md overflow-x-auto text-xs font-mono shadow-inner">
                            <div className="text-gray-400 mb-2 flex justify-between uppercase text-[10px] font-bold tracking-wider">
                              <span>Generated SQL</span>
                            </div>
                            <code>{msg.sql}</code>
                          </div>
                        )}

                        {msg.retrieval && (
                          <div className="bg-blue-50 border border-blue-200 rounded-md p-3 text-xs text-blue-900 space-y-1">
                            <div>
                              检索模式: {msg.retrieval.fallback_mode || 'unknown'}
                              {typeof msg.retrieval.top_k === 'number' ? ` | top_k=${msg.retrieval.top_k}` : ''}
                              {typeof msg.retrieval.score_threshold === 'number'
                                ? ` | threshold=${msg.retrieval.score_threshold}`
                                : ''}
                            </div>
                            <div>
                              命中表: {(msg.retrieval.selected_tables || []).join(', ') || '无'}
                            </div>
                            {!!msg.retrieval.scores?.length && (
                              <div className="space-y-1">
                                {(msg.retrieval.scores || []).map((item, idx) => (
                                  <div key={`${item.table_name}-${idx}`}>
                                    {item.table_name}: score={item.score}
                                    {typeof item.raw_score === 'number' ? `, raw=${item.raw_score}` : ''}
                                  </div>
                                ))}
                              </div>
                            )}
                          </div>
                        )}

                        {msg.columns && msg.rows && msg.rows.length > 0 && (
                          <div className="overflow-x-auto border rounded-lg shadow-sm mt-4">
                            <table className="min-w-full divide-y divide-gray-200 text-xs">
                              <thead className="bg-gray-50">
                                <tr>
                                  {msg.columns.map((col, i) => (
                                    <th key={i} className="px-4 py-3 text-left font-semibold text-gray-600 uppercase tracking-wider whitespace-nowrap">
                                      {col}
                                    </th>
                                  ))}
                                </tr>
                              </thead>
                              <tbody className="bg-white divide-y divide-gray-200">
                                {msg.rows.map((row, i) => (
                                  <tr key={i} className="hover:bg-blue-50 transition-colors">
                                    {row.map((cell, j) => (
                                      <td key={j} className="px-4 py-2 whitespace-nowrap text-gray-800">
                                        {cell !== null ? String(cell) : <span className="text-gray-400 italic">NULL</span>}
                                      </td>
                                    ))}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}

                        {(msg.chartConfig != null || msg.chartError || msg.echartsCode) && (
                          <div className="mt-4 border rounded-lg p-3 bg-white">
                            {(() => {
                              if (msg.chartError) {
                                return (
                                  <div role="status" className="text-xs text-gray-600">
                                    图表未生成：{msg.chartError}
                                  </div>
                                );
                              }
                              if (msg.chartConfig == null) {
                                return (
                                  <div role="status" className="text-xs text-gray-600">
                                    此历史图表使用旧版配置，已停止加载。重新查询可生成图表。
                                  </div>
                                );
                              }
                              if (!msg.rows?.length) {
                                return (
                                  <div role="status" className="text-xs text-gray-600">
                                    没有可展示的图表数据。
                                  </div>
                                );
                              }
                              const option = buildChartOption(msg.chartConfig, msg.rows, msg.columns);
                              if (!option) {
                                return (
                                  <div role="status" className="text-xs text-red-500">
                                    图表配置无效，查询结果已保留。请重新查询。
                                  </div>
                                );
                              }
                              return (
                                <ReactECharts
                                  option={option}
                                  style={{ height: 360, width: '100%' }}
                                  notMerge={true}
                                  lazyUpdate={true}
                                />
                              );
                            })()}
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-white via-white to-transparent pt-10 pb-6 px-4 md:px-8">
          <div className="max-w-4xl mx-auto">
            <form onSubmit={handleSubmit} className="relative bg-gray-100 rounded-2xl shadow-sm border border-gray-200 focus-within:border-gray-300 focus-within:bg-white transition-colors duration-200 px-4 py-3">
              <div className="flex items-end">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSubmit(e);
                    }
                  }}
                  placeholder="有问题，尽管问... (Shift + Enter 换行)"
                  className="flex-1 bg-transparent border-none outline-none py-1 text-gray-800 text-sm resize-none max-h-32 min-h-[24px] overflow-y-auto"
                  rows={1}
                />
                <button
                  type="submit"
                  disabled={!input.trim()}
                  className={`ml-3 mb-0.5 p-2 rounded-full flex items-center justify-center transition-colors flex-shrink-0 ${
                    input.trim() ? 'bg-orange-500 text-white hover:bg-orange-600 shadow-md' : 'bg-gray-300 text-gray-100'
                  }`}
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 10l7-7m0 0l7 7m-7-7v18" />
                  </svg>
                </button>
              </div>
              <div className="mt-2 flex items-center">
                <button
                  type="button"
                  onClick={() => {
                    const next = !includeChart;
                    setIncludeChart(next);
                    localStorage.setItem('include_chart', String(next));
                  }}
                  className={`text-xs px-3 py-1 rounded-full border transition ${
                    includeChart
                      ? 'bg-blue-500 text-white border-blue-500'
                      : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
                  }`}
                >
                  可视化展示
                </button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;
