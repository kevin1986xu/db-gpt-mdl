import {
  MdlKnowledgeCorpus,
  MdlKnowledgeEntry,
  MdlKnowledgeExpectedResult,
  MdlKnowledgeKind,
  MdlKnowledgeRuleCheck,
  MdlKnowledgeSearchMatch,
  MdlModel,
  approveMdlKnowledge,
  getMdlKnowledge,
  saveMdlKnowledge,
  searchMdlKnowledge,
} from '@/client/api/mdl';
import { apiInterceptors } from '@/client/api/tools/interceptors';
import {
  CheckCircleOutlined,
  DeleteOutlined,
  DownloadOutlined,
  EditOutlined,
  PlusOutlined,
  ReloadOutlined,
  SaveOutlined,
  SearchOutlined,
} from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import MdlKnowledgeSpaces from './MdlKnowledgeSpaces';

type Props = {
  database?: string;
  models: MdlModel[];
};

type KnowledgeFormValues = {
  id?: string;
  kind: MdlKnowledgeKind;
  title: string;
  question?: string;
  content: string;
  sql?: string;
  aliasesText?: string;
  modelNames: string[];
  fieldsByModel?: Record<string, string[]>;
  terms?: Array<{ term?: string; meaning?: string; aliasesText?: string }>;
  ruleMetric?: string;
  ruleConditionsText?: string;
  ruleGuidance?: string;
  ruleReportRequirementsText?: string;
  ruleChecks?: Array<{
    column: string;
    operator: string;
    valueText?: string;
    severity?: 'error' | 'warning';
    message?: string;
  }>;
  expectedResultColumnsText?: string;
  expectedResultRowsText?: string;
  reuseOnExactMatch?: boolean;
};

const emptyCorpus: MdlKnowledgeCorpus = { entries: [] };

const checkOperators = [
  { value: 'eq', label: '等于' },
  { value: 'ne', label: '不等于' },
  { value: 'gt', label: '大于' },
  { value: 'gte', label: '大于等于' },
  { value: 'lt', label: '小于' },
  { value: 'lte', label: '小于等于' },
  { value: 'between', label: '处于范围内' },
  { value: 'in', label: '属于枚举集合' },
  { value: 'not_null', label: '非空' },
  { value: 'is_null', label: '为空' },
];

function parseRuleChecks(values: KnowledgeFormValues): MdlKnowledgeRuleCheck[] {
  const parseScalar = (text: string): string | number | boolean | null => {
    const trimmed = text.trim();
    try {
      const value: unknown = JSON.parse(trimmed);
      if (
        value === null ||
        typeof value === 'string' ||
        typeof value === 'boolean' ||
        (typeof value === 'number' && Number.isFinite(value))
      )
        return value;
    } catch {
      /* Plain text enum values do not need quotes. */
    }
    return trimmed;
  };
  return (values.ruleChecks ?? []).map(check => {
    const result: MdlKnowledgeRuleCheck = {
      column: check.column.trim(),
      operator: check.operator,
      severity: check.severity ?? 'error',
      message: check.message?.trim() ?? '',
    };
    if (['not_null', 'is_null'].includes(check.operator)) return result;
    if (!check.valueText?.trim()) throw new Error('请填写每个比较断言的期望值');
    if (['between', 'in'].includes(check.operator)) {
      if (check.valueText.trim().startsWith('[')) {
        const parsed: unknown = JSON.parse(check.valueText);
        if (
          !Array.isArray(parsed) ||
          parsed.some(
            value =>
              value !== null &&
              typeof value !== 'string' &&
              typeof value !== 'boolean' &&
              !(typeof value === 'number' && Number.isFinite(value)),
          )
        ) {
          throw new Error('枚举期望值只能包含文字、数字、布尔值或空值');
        }
        result.value = parsed;
      } else {
        result.value = check.valueText.split(/[,，\n]/).map(parseScalar);
      }
      if (
        check.operator === 'between' &&
        (result.value.length !== 2 || result.value.some(value => typeof value !== 'number'))
      ) {
        throw new Error('范围断言需要两个数字，例如：0,100');
      }
    } else {
      result.value = parseScalar(check.valueText);
      if (['gt', 'gte', 'lt', 'lte'].includes(check.operator) && typeof result.value !== 'number') {
        throw new Error('大小比较断言需要数值');
      }
    }
    return result;
  });
}

function modelLabel(model: MdlModel) {
  return model.display_name || model.name;
}

function optionalModelLabel(modelName: string, model?: MdlModel) {
  return model ? modelLabel(model) : modelName;
}

function columnLabel(column: NonNullable<MdlModel['columns']>[number]) {
  return column.display_name ? `${column.display_name} (${column.name})` : column.name;
}

function splitAliases(value?: string) {
  return (value ?? '')
    .split(/[\n,，]/)
    .map(item => item.trim())
    .filter(Boolean);
}

function joinAliases(value?: string[]) {
  return (value ?? []).join('\n');
}

function normalizeEntries(entries?: MdlKnowledgeEntry[]) {
  return (entries ?? []).map(entry => ({
    ...entry,
    aliases: entry.aliases ?? [],
    models: entry.models ?? {},
    terms: entry.terms ?? [],
    rule: entry.rule,
    status: entry.status ?? 'draft',
  }));
}

function toSaveEntry(entry: MdlKnowledgeEntry): MdlKnowledgeEntry {
  const {
    stale: _stale,
    validation: _validation,
    exact_match: _exactMatch,
    conflict: _conflict,
    score: _score,
    ...payload
  } = entry;
  return {
    ...payload,
    terms: entry.kind === 'glossary' ? (entry.terms ?? []) : [],
    rule: entry.kind === 'analysis_rule' ? entry.rule : undefined,
  };
}

function createEntryId() {
  if (typeof crypto !== 'undefined') {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    if (typeof crypto.getRandomValues === 'function') {
      const bytes = new Uint8Array(16);
      crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
  }
  const hex = `${Date.now().toString(16).padStart(12, '0')}${Math.random().toString(16).slice(2)}${Math.random()
    .toString(16)
    .slice(2)}`
    .padEnd(32, '0')
    .slice(0, 32)
    .split('');
  hex[12] = '4';
  hex[16] = '8';
  const value = hex.join('');
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`;
}

const kindLabels: Record<MdlKnowledgeKind, string> = {
  qa_sql: '问答 SQL',
  business_rule: '业务口径',
  glossary: '字典术语',
  analysis_rule: '分析规则',
};

function kindLabel(kind: MdlKnowledgeKind) {
  return kindLabels[kind] ?? kind;
}

function statusTag(entry: MdlKnowledgeEntry) {
  if (entry.stale) return <Tag color='orange'>需重新审核</Tag>;
  if (entry.status === 'approved') return <Tag color='green'>已审核</Tag>;
  return <Tag>草稿</Tag>;
}

function validationTag(entry: MdlKnowledgeEntry) {
  const mode = entry.validation?.mode;
  if (!mode) return <Tag>未验证</Tag>;
  if (mode === 'result_checked') return <Tag color='green'>结果已校验</Tag>;
  if (mode === 'wren_plan_explain') return <Tag color='blue'>计划已解释</Tag>;
  if (mode === 'wren_plan') return <Tag color='cyan'>计划已校验</Tag>;
  return <Tag color='blue'>{mode}</Tag>;
}

function splitLines(value?: string) {
  return (value ?? '')
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean);
}

function joinLines(value?: string[]) {
  return (value ?? []).join('\n');
}

function parseExpectedResult(values: KnowledgeFormValues): MdlKnowledgeExpectedResult | undefined {
  const columns = splitAliases(values.expectedResultColumnsText);
  const rawRows = values.expectedResultRowsText?.trim();
  if (!columns.length && !rawRows) return undefined;
  if (!columns.length || !rawRows) {
    throw new Error('期望结果需要同时填写列名和 JSON 行数据。');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(rawRows);
  } catch {
    throw new Error('期望结果行数据必须是合法 JSON 数组。');
  }
  if (!Array.isArray(parsed) || parsed.length > 100 || !parsed.every(row => Array.isArray(row))) {
    throw new Error('期望结果必须是最多 100 行的二维 JSON 数组。');
  }
  const rows = parsed as unknown[][];
  if (
    rows.some(
      row =>
        row.length !== columns.length ||
        row.some(
          value =>
            value !== null && typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean',
        ),
    )
  ) {
    throw new Error('期望结果每行的列数必须与列名一致，单元格只能是字符串、数字、布尔值或 null。');
  }
  return { columns, rows: rows as MdlKnowledgeExpectedResult['rows'] };
}

function downloadJson(filename: string, value: unknown) {
  const blob = new Blob([JSON.stringify(value, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function MdlKnowledge({ database, models }: Props) {
  const [corpus, setCorpus] = useState<MdlKnowledgeCorpus>(emptyCorpus);
  const [entries, setEntries] = useState<MdlKnowledgeEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [approvingId, setApprovingId] = useState<string>();
  const [dirty, setDirty] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string>();
  const [searchQuestion, setSearchQuestion] = useState('');
  const [searching, setSearching] = useState(false);
  const [matches, setMatches] = useState<MdlKnowledgeSearchMatch[]>([]);
  const [form] = Form.useForm<KnowledgeFormValues>();
  const loadId = useRef(0);
  const requestGeneration = useRef(0);
  const activeDatabase = useRef(database);
  const previousDatabase = useRef(database);
  activeDatabase.current = database;
  const selectedKind = Form.useWatch('kind', form);
  const selectedModelNames = ((Form.useWatch('modelNames', form) ?? []) as string[]).filter(Boolean);
  const hasLoadedVersion = typeof corpus.revision === 'number';
  const actionDisabled = !database || loading || saving || approvingId !== undefined || !hasLoadedVersion;
  const refreshDisabled = loading || saving || approvingId !== undefined;

  const modelOptions = useMemo(() => models.map(model => ({ label: modelLabel(model), value: model.name })), [models]);
  const modelMap = useMemo(
    () =>
      models.reduce<Record<string, MdlModel>>((acc, model) => {
        acc[model.name] = model;
        return acc;
      }, {}),
    [models],
  );

  useEffect(() => {
    activeDatabase.current = database;
    if (previousDatabase.current !== database) {
      requestGeneration.current += 1;
      previousDatabase.current = database;
      setCorpus(database ? { database, entries: [] } : emptyCorpus);
      setEntries([]);
      setDirty(false);
      setMatches([]);
      setSaving(false);
      setSearching(false);
      setApprovingId(undefined);
      setModalOpen(false);
      setEditingId(undefined);
    }
  }, [database]);

  const loadKnowledge = useCallback(async () => {
    const currentLoadId = ++loadId.current;
    const requestDatabase = database;
    if (!requestDatabase) {
      setCorpus(emptyCorpus);
      setEntries([]);
      setDirty(false);
      setMatches([]);
      return;
    }
    setLoading(true);
    try {
      const [error, result, response] = await apiInterceptors(getMdlKnowledge(requestDatabase));
      if (currentLoadId !== loadId.current || requestDatabase !== activeDatabase.current) return;
      if (error || !response?.success || !result) {
        return;
      }
      const nextEntries = normalizeEntries(result.entries);
      setCorpus({ ...result, entries: nextEntries });
      setEntries(nextEntries);
      setDirty(false);
      setMatches([]);
    } finally {
      if (currentLoadId === loadId.current) setLoading(false);
    }
  }, [database]);

  useEffect(() => {
    loadKnowledge();
  }, [loadKnowledge]);

  const currentKnowledge = useCallback(
    () => ({
      database,
      revision: corpus.revision,
      entries,
    }),
    [corpus.revision, database, entries],
  );

  const handleExportJson = () => {
    downloadJson(`${database || 'mdl'}-knowledge.json`, currentKnowledge());
  };

  const handleRefresh = () => {
    if (!dirty) {
      loadKnowledge();
      return;
    }
    Modal.confirm({
      title: '备份并刷新知识库',
      content:
        '当前有未保存的知识编辑。继续后会先下载一份本地 JSON 备份，然后加载服务器上的最新知识内容；这个操作不会自动覆盖服务器，也不会自动修改当前服务器版本。',
      okText: '备份并刷新',
      cancelText: '取消',
      onOk: () => {
        handleExportJson();
        loadKnowledge();
      },
    });
  };

  const saveEntries = async (nextEntries: MdlKnowledgeEntry[], generation: number) => {
    const requestDatabase = database;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return undefined;
    }
    const [error, saved, response] = await apiInterceptors(
      saveMdlKnowledge({ database: requestDatabase, revision: corpus.revision, entries: nextEntries.map(toSaveEntry) }),
    );
    if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return undefined;
    if (error || !response?.success || !saved) {
      const detail =
        (response as { err_msg?: string; detail?: string } | null)?.err_msg ??
        (response as { err_msg?: string; detail?: string } | null)?.detail ??
        (error instanceof Error ? error.message : '');
      message.error(detail || '知识保存失败，当前编辑已保留，请核对版本后重试。');
      return undefined;
    }
    const normalized = normalizeEntries(saved.entries);
    setCorpus({ ...saved, entries: normalized });
    setEntries(normalized);
    setDirty(false);
    return { ...saved, entries: normalized };
  };

  const handleSaveAll = async () => {
    const requestDatabase = database;
    const generation = ++requestGeneration.current;
    setSaving(true);
    try {
      const saved = await saveEntries(entries, generation);
      if (saved && requestDatabase === activeDatabase.current && generation === requestGeneration.current) {
        setMatches([]);
        message.success('知识库已保存');
      }
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current) setSaving(false);
    }
  };

  const openCreate = () => {
    setEditingId(undefined);
    form.resetFields();
    form.setFieldsValue({ kind: 'qa_sql', modelNames: [], terms: [], reuseOnExactMatch: false });
    setModalOpen(true);
  };

  const openEdit = (entry: MdlKnowledgeEntry) => {
    if (saving || approvingId !== undefined) return;
    setEditingId(entry.id);
    form.setFieldsValue({
      id: entry.id,
      kind: entry.kind,
      title: entry.title,
      question: entry.question,
      content: entry.content,
      sql: entry.sql,
      aliasesText: joinAliases(entry.aliases),
      modelNames: Object.keys(entry.models ?? {}),
      fieldsByModel: entry.models,
      terms: (entry.terms ?? []).map(term => ({
        term: term.term,
        meaning: term.meaning,
        aliasesText: joinAliases(term.aliases),
      })),
      ruleMetric: entry.rule?.metric,
      ruleConditionsText: joinLines(entry.rule?.conditions),
      ruleGuidance: entry.rule?.guidance,
      ruleReportRequirementsText: joinLines(entry.rule?.report_requirements),
      ruleChecks: (entry.rule?.checks ?? []).map(check => ({
        ...check,
        valueText: check.value === undefined ? undefined : JSON.stringify(check.value),
      })),
      expectedResultColumnsText: joinAliases(entry.expected_result?.columns),
      expectedResultRowsText: entry.expected_result ? JSON.stringify(entry.expected_result.rows, null, 2) : undefined,
      reuseOnExactMatch: entry.reuse_on_exact_match ?? false,
    });
    setModalOpen(true);
  };

  const handleDelete = (entry: MdlKnowledgeEntry) => {
    if (saving || approvingId !== undefined) return;
    Modal.confirm({
      title: '删除知识条目',
      content: `删除「${entry.title}」后需要点击保存才会写入后端。`,
      okText: '删除',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: () => {
        setEntries(prev => prev.filter(item => item.id !== entry.id));
        setDirty(true);
      },
    });
  };

  const handleSubmitEntry = async () => {
    const values = await form.validateFields();
    let expectedResult: MdlKnowledgeExpectedResult | undefined;
    let ruleChecks: MdlKnowledgeRuleCheck[] = [];
    try {
      expectedResult = values.kind === 'qa_sql' ? parseExpectedResult(values) : undefined;
      if (values.kind === 'analysis_rule') ruleChecks = parseRuleChecks(values);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '期望结果格式不正确');
      return;
    }
    const bindings = values.modelNames.reduce<Record<string, string[]>>((acc, modelName) => {
      const fields = values.fieldsByModel?.[modelName] ?? [];
      if (fields.length > 0) acc[modelName] = fields;
      return acc;
    }, {});
    if (Object.keys(bindings).length === 0 || Object.keys(bindings).length !== values.modelNames.length) {
      message.warning('请为每个绑定模型至少选择一个字段');
      return;
    }
    if (values.kind === 'glossary' && !values.terms?.length) {
      message.warning('至少添加一个术语条目');
      return;
    }
    if (values.kind === 'analysis_rule' && !values.ruleMetric?.trim()) {
      message.warning('请输入分析指标');
      return;
    }
    const nextEntry: MdlKnowledgeEntry = {
      id: editingId ?? values.id ?? createEntryId(),
      kind: values.kind,
      title: values.title.trim(),
      question: values.kind === 'qa_sql' ? values.question?.trim() : values.question?.trim() || undefined,
      content: values.content.trim(),
      sql: values.kind === 'qa_sql' ? values.sql?.trim() : undefined,
      aliases: splitAliases(values.aliasesText),
      models: bindings,
      terms:
        values.kind === 'glossary'
          ? (values.terms ?? []).map(term => ({
              term: term.term?.trim() ?? '',
              meaning: term.meaning?.trim() ?? '',
              aliases: splitAliases(term.aliasesText),
            }))
          : [],
      rule:
        values.kind === 'analysis_rule'
          ? {
              metric: values.ruleMetric?.trim() ?? '',
              conditions: splitLines(values.ruleConditionsText),
              guidance: values.ruleGuidance?.trim() ?? '',
              checks: ruleChecks,
              report_requirements: splitLines(values.ruleReportRequirementsText),
            }
          : undefined,
      expected_result: expectedResult,
      reuse_on_exact_match: values.kind === 'qa_sql' ? values.reuseOnExactMatch === true : false,
      status: 'draft',
    };
    setEntries(prev => {
      const next = editingId ? prev.map(item => (item.id === editingId ? nextEntry : item)) : [...prev, nextEntry];
      return next;
    });
    setDirty(true);
    setModalOpen(false);
  };

  const handleApprove = async (entry: MdlKnowledgeEntry) => {
    const requestDatabase = database;
    const generation = ++requestGeneration.current;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return;
    }
    setApprovingId(entry.id);
    try {
      const saved = await saveEntries(entries, generation);
      if (!saved) return;
      const [error, approved, response] = await apiInterceptors(
        approveMdlKnowledge({ database: requestDatabase, id: entry.id, revision: saved.revision }),
      );
      if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return;
      if (error || !response?.success || !approved) {
        message.error('审核失败，条目仍保留为草稿或需重新审核状态。');
        return;
      }
      const normalized = normalizeEntries(approved.entries);
      setCorpus({ ...approved, entries: normalized });
      setEntries(normalized);
      setDirty(false);
      setMatches([]);
      message.success('知识条目已审核');
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current)
        setApprovingId(undefined);
    }
  };

  const handleSearch = async () => {
    const requestDatabase = database;
    const generation = requestGeneration.current;
    if (!requestDatabase) {
      message.warning('请先选择数据源');
      return;
    }
    if (!searchQuestion.trim()) {
      message.warning('请输入测试问题');
      return;
    }
    setSearching(true);
    try {
      const [error, result, response] = await apiInterceptors(
        searchMdlKnowledge({ database: requestDatabase, question: searchQuestion.trim(), top_k: 4 }),
      );
      if (requestDatabase !== activeDatabase.current || generation !== requestGeneration.current) return;
      if (error || !response?.success || !result) return;
      setMatches(result.matches ?? []);
    } finally {
      if (requestDatabase === activeDatabase.current && generation === requestGeneration.current) setSearching(false);
    }
  };

  const columns: ColumnsType<MdlKnowledgeEntry> = [
    {
      title: '知识',
      dataIndex: 'title',
      width: 230,
      render: (_, entry) => (
        <div>
          <div className='font-medium'>{entry.title}</div>
          <div className='text-xs text-gray-400'>
            {kindLabel(entry.kind)}
            {entry.question ? ` · ${entry.question}` : ''}
          </div>
        </div>
      ),
    },
    {
      title: '状态',
      width: 110,
      render: (_, entry) => statusTag(entry),
    },
    {
      title: '校验',
      width: 125,
      render: (_, entry) => validationTag(entry),
    },
    {
      title: '绑定模型字段',
      width: 210,
      render: (_, entry) => (
        <Space wrap size={[4, 4]}>
          {Object.entries(entry.models).map(([modelName, fields]) => (
            <Tag key={modelName}>
              {modelName}:{fields.length}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '别名',
      dataIndex: 'aliases',
      width: 220,
      render: value => (value?.length ? value.join(' / ') : '-'),
    },
    {
      title: 'SQL 示例',
      dataIndex: 'sql',
      width: 300,
      ellipsis: true,
      render: value => value || '-',
    },
    {
      title: '操作',
      width: 210,
      render: (_, entry) => (
        <Space>
          <Button
            type='link'
            icon={<EditOutlined />}
            disabled={saving || approvingId !== undefined}
            onClick={() => openEdit(entry)}
          >
            编辑
          </Button>
          <Button
            type='link'
            icon={<CheckCircleOutlined />}
            loading={approvingId === entry.id}
            disabled={actionDisabled}
            onClick={() => handleApprove(entry)}
          >
            审核
          </Button>
          <Button
            danger
            type='link'
            icon={<DeleteOutlined />}
            disabled={saving || approvingId !== undefined}
            onClick={() => handleDelete(entry)}
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  const matchColumns: ColumnsType<MdlKnowledgeSearchMatch> = [
    {
      title: '命中',
      dataIndex: 'title',
      render: (_, entry) => (
        <div>
          <div className='font-medium'>{entry.title}</div>
          <Space size={4} wrap>
            <Tag>{kindLabel(entry.kind)}</Tag>
            {entry.exact_match && <Tag color='green'>精确问法</Tag>}
            {entry.conflict && <Tag color='red'>命中冲突</Tag>}
          </Space>
          <Typography.Paragraph className='!mb-0 text-xs text-gray-500' ellipsis={{ rows: 2 }}>
            {entry.content}
          </Typography.Paragraph>
        </div>
      ),
    },
    {
      title: 'Score',
      width: 90,
      render: (_, entry) => (typeof entry.score === 'number' ? entry.score.toFixed(3) : '-'),
    },
    {
      title: '状态',
      width: 110,
      render: (_, entry) => statusTag(entry),
    },
    {
      title: '校验',
      width: 125,
      render: (_, entry) => validationTag(entry),
    },
    {
      title: 'SQL',
      dataIndex: 'sql',
      ellipsis: true,
      render: value => value || '-',
    },
  ];

  return (
    <Space direction='vertical' className='w-full' size='middle'>
      <Alert
        type='info'
        showIcon
        message='知识条目用于提升问数命中率，不保存固定数值答案'
        description='请先保存 MDL 模型和字段，再审核知识条目。审核会绑定当前已保存 MDL 指纹；MDL 变化后，后端可将旧知识标记为需重新审核。'
      />
      <Card
        size='small'
        title={
          <Space>
            <span>知识条目</span>
            <Tag>版本 {corpus.revision ?? '未加载'}</Tag>
            {dirty && <Tag color='orange'>未保存</Tag>}
          </Space>
        }
        extra={
          <Space wrap>
            <Button icon={<DownloadOutlined />} disabled={!database || loading} onClick={handleExportJson}>
              导出知识 JSON
            </Button>
            <Button icon={<ReloadOutlined />} loading={loading} disabled={refreshDisabled} onClick={handleRefresh}>
              {dirty ? '备份并刷新' : '刷新'}
            </Button>
            <Button icon={<PlusOutlined />} type='primary' ghost disabled={actionDisabled} onClick={openCreate}>
              新增知识
            </Button>
            <Button
              type='primary'
              icon={<SaveOutlined />}
              loading={saving}
              disabled={actionDisabled || !dirty}
              onClick={handleSaveAll}
            >
              保存列表
            </Button>
          </Space>
        }
      >
        <Table
          rowKey='id'
          size='small'
          loading={loading}
          dataSource={entries}
          columns={columns}
          scroll={{ x: 1400 }}
          pagination={{ pageSize: 8 }}
          locale={{ emptyText: <Empty description={database ? '暂无知识条目' : '请先选择数据源'} /> }}
        />
      </Card>

      <Card size='small' title='搜索预览'>
        <Space.Compact className='w-full'>
          <Input
            value={searchQuestion}
            onChange={event => setSearchQuestion(event.target.value)}
            onPressEnter={handleSearch}
            placeholder='输入用户可能提出的问题，预览 top 4 知识命中'
          />
          <Button
            icon={<SearchOutlined />}
            loading={searching}
            disabled={!database || loading || saving || approvingId !== undefined}
            onClick={handleSearch}
          >
            搜索
          </Button>
        </Space.Compact>
        <Table
          className='mt-3'
          rowKey='id'
          size='small'
          pagination={false}
          dataSource={matches}
          columns={matchColumns}
          locale={{ emptyText: <Empty description='暂无检索结果' /> }}
        />
      </Card>

      <MdlKnowledgeSpaces database={database} models={models} />

      <Modal
        title={editingId ? '编辑知识条目' : '新增知识条目'}
        open={modalOpen}
        width={820}
        okText='保存到本地列表'
        cancelText='取消'
        destroyOnClose
        onCancel={() => setModalOpen(false)}
        onOk={handleSubmitEntry}
      >
        <Form form={form} layout='vertical' initialValues={{ kind: 'qa_sql', modelNames: [], terms: [] }}>
          <Form.Item name='kind' label='类型' rules={[{ required: true, message: '请选择类型' }]}>
            <Select options={[...Object.entries(kindLabels).map(([value, label]) => ({ label, value }))]} />
          </Form.Item>
          <Form.Item name='title' label='标题' rules={[{ required: true, message: '请输入标题' }]}>
            <Input placeholder='例如：活跃无人机数量口径' />
          </Form.Item>
          <Form.Item
            name='question'
            label='典型问题'
            rules={selectedKind === 'qa_sql' ? [{ required: true, message: '请输入典型问题' }] : undefined}
          >
            <Input placeholder='例如：当前有多少架无人机？' />
          </Form.Item>
          <Form.Item name='content' label='口径 / 内容' rules={[{ required: true, message: '请输入口径或内容' }]}>
            <Input.TextArea rows={4} placeholder='描述适用范围、过滤条件、指标口径和注意事项，不填写固定数值答案' />
          </Form.Item>
          {selectedKind === 'qa_sql' && (
            <Form.Item
              name='sql'
              label='SQL 示例'
              rules={selectedKind === 'qa_sql' ? [{ required: true, message: '请输入 SQL 示例' }] : undefined}
            >
              <Input.TextArea rows={4} placeholder='select count(*) from device_registration ...' />
            </Form.Item>
          )}
          {selectedKind === 'qa_sql' && (
            <>
              <Form.Item name='reuseOnExactMatch' valuePropName='checked'>
                <Checkbox>精确问法复用审核 SQL</Checkbox>
              </Form.Item>
              <Typography.Paragraph type='secondary' className='!mb-3 text-xs'>
                完整问题或别名在规范化标点后完全一致，且没有不同审核 SQL 的冲突时，运行时才会复用审核口径。
              </Typography.Paragraph>
              <Form.Item label='期望结果列名'>
                <Form.Item name='expectedResultColumnsText' noStyle>
                  <Input placeholder='例如：设备数量, 状态' />
                </Form.Item>
              </Form.Item>
              <Form.Item
                name='expectedResultRowsText'
                label='期望结果行（JSON）'
                rules={[
                  {
                    validator: async (_, value: string | undefined) => {
                      if (!value?.trim()) return;
                      try {
                        const parsed = JSON.parse(value);
                        if (
                          !Array.isArray(parsed) ||
                          parsed.length > 100 ||
                          parsed.some(
                            (row: unknown) =>
                              !Array.isArray(row) ||
                              row.some(
                                (cell: unknown) =>
                                  cell !== null &&
                                  typeof cell !== 'string' &&
                                  typeof cell !== 'number' &&
                                  typeof cell !== 'boolean',
                              ),
                          )
                        ) {
                          throw new Error();
                        }
                      } catch {
                        throw new Error('请输入最多 100 行的二维 JSON 数组，单元格只能是标量或 null');
                      }
                    },
                  },
                ]}
              >
                <Input.TextArea rows={4} placeholder={'例如：[[12, "active"], [8, "paused"]]'} spellCheck={false} />
              </Form.Item>
            </>
          )}
          {selectedKind === 'glossary' && (
            <Form.Item label='术语条目' required>
              <Form.List name='terms'>
                {(fields, { add, remove }) => (
                  <Space direction='vertical' className='w-full' size='small'>
                    {fields.map(field => (
                      <Space key={field.key} align='start' className='w-full' wrap>
                        <Form.Item
                          {...field}
                          name={[field.name, 'term']}
                          rules={[{ required: true, message: '请输入术语' }]}
                          className='!mb-0 min-w-[150px] flex-1'
                        >
                          <Input placeholder='术语' />
                        </Form.Item>
                        <Form.Item
                          {...field}
                          name={[field.name, 'meaning']}
                          rules={[{ required: true, message: '请输入术语含义' }]}
                          className='!mb-0 min-w-[240px] flex-[2]'
                        >
                          <Input placeholder='业务含义' />
                        </Form.Item>
                        <Form.Item {...field} name={[field.name, 'aliasesText']} className='!mb-0 min-w-[180px] flex-1'>
                          <Input placeholder='别名（逗号分隔）' />
                        </Form.Item>
                        <Button
                          danger
                          type='text'
                          icon={<DeleteOutlined />}
                          aria-label='删除术语'
                          onClick={() => remove(field.name)}
                        />
                      </Space>
                    ))}
                    <Button type='dashed' icon={<PlusOutlined />} onClick={() => add({ aliasesText: '' })}>
                      添加术语
                    </Button>
                  </Space>
                )}
              </Form.List>
            </Form.Item>
          )}
          {selectedKind === 'analysis_rule' && (
            <>
              <Form.Item name='ruleMetric' label='指标' rules={[{ required: true, message: '请输入指标' }]}>
                <Input placeholder='例如：活跃无人机数量' />
              </Form.Item>
              <Form.Item name='ruleConditionsText' label='判断条件（每行一条）'>
                <Input.TextArea
                  rows={3}
                  placeholder='例如：设备状态为 active&#10;按设备 SN 去重'
                />
              </Form.Item>
              <Form.Item name='ruleGuidance' label='分析指导'>
                <Input.TextArea rows={3} placeholder='描述异常判断、同比环比或报告输出要求' />
              </Form.Item>
              <Form.Item label='结果断言'>
                <Typography.Paragraph type='secondary' className='text-xs'>
                  按本次 SQL 返回列逐行验证。缺列或空结果会标记为未能校验，文字判断条件不会自动执行。
                </Typography.Paragraph>
                <Form.List name='ruleChecks'>
                  {(fields, { add, remove }) => (
                    <Space direction='vertical' className='w-full'>
                      {fields.map(field => (
                        <Card key={field.key} size='small'>
                          <Space wrap align='start'>
                            <Form.Item
                              name={[field.name, 'column']}
                              rules={[{ required: true, message: '请输入结果列名' }]}
                            >
                              <Input placeholder='SQL 结果列名' />
                            </Form.Item>
                            <Form.Item
                              name={[field.name, 'operator']}
                              rules={[{ required: true, message: '请选择比较方式' }]}
                            >
                              <Select className='min-w-[140px]' options={checkOperators} placeholder='比较方式' />
                            </Form.Item>
                            <Form.Item name={[field.name, 'valueText']}>
                              <Input placeholder='期望值；范围/枚举用逗号分隔' />
                            </Form.Item>
                            <Button
                              danger
                              type='text'
                              icon={<DeleteOutlined />}
                              aria-label='删除结果断言'
                              onClick={() => remove(field.name)}
                            />
                          </Space>
                          <Space wrap align='start'>
                            <Form.Item name={[field.name, 'severity']} className='!mb-0'>
                              <Select
                                className='min-w-[100px]'
                                options={[
                                  { value: 'error', label: '错误' },
                                  { value: 'warning', label: '提醒' },
                                ]}
                              />
                            </Form.Item>
                            <Form.Item name={[field.name, 'message']} className='!mb-0'>
                              <Input placeholder='判定说明，例如：金额不能为负' />
                            </Form.Item>
                          </Space>
                        </Card>
                      ))}
                      <Button
                        type='dashed'
                        icon={<PlusOutlined />}
                        onClick={() => add({ operator: 'gte', severity: 'error' })}
                      >
                        添加结果断言
                      </Button>
                    </Space>
                  )}
                </Form.List>
              </Form.Item>
              <Form.Item name='ruleReportRequirementsText' label='报告要求（每行一条）'>
                <Input.TextArea rows={3} placeholder='例如：注明统计时间范围和单位；列出异常记录；注明引用口径' />
              </Form.Item>
            </>
          )}
          <Form.Item name='aliasesText' label='别名 / 近义词'>
            <Input.TextArea rows={2} placeholder='每行一个，或用逗号分隔' />
          </Form.Item>
          <Form.Item name='modelNames' label='绑定模型' rules={[{ required: true, message: '请选择绑定模型' }]}>
            <Select mode='multiple' options={modelOptions} placeholder='选择知识适用的 MDL 模型' />
          </Form.Item>
          {selectedModelNames.map(modelName => {
            const model = modelMap[modelName];
            const options = (model?.columns ?? []).map(column => ({
              label: columnLabel(column),
              value: column.name,
            }));
            return (
              <Form.Item
                key={modelName}
                name={['fieldsByModel', modelName]}
                label={`${optionalModelLabel(modelName, model)} 字段`}
              >
                <Select mode='multiple' options={options} placeholder='至少选择一个字段' />
              </Form.Item>
            );
          })}
        </Form>
      </Modal>
    </Space>
  );
}

export default MdlKnowledge;
