import {
  MdlKnowledgeSpaceBinding,
  MdlKnowledgeSpaceCorpus,
  MdlModel,
  approveMdlKnowledgeSpace,
  getMdlKnowledgeSpaces,
  saveMdlKnowledgeSpaces,
} from '@/client/api/mdl';
import { apiInterceptors } from '@/client/api/tools/interceptors';
import { CheckCircleOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Form, Input, Modal, Select, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useRef, useState } from 'react';

type Props = { database?: string; models: MdlModel[] };
type BindingValues = {
  title: string;
  space_name: string;
  description?: string;
  modelNames: string[];
  fieldsByModel?: Record<string, string[]>;
  usersText?: string;
  roles?: string[];
};
const splitUsers = (value?: string) =>
  (value ?? '')
    .split(/[,，\n]/)
    .map(user => user.trim())
    .filter(Boolean);

export default function MdlKnowledgeSpaces({ database, models }: Props) {
  const [corpus, setCorpus] = useState<MdlKnowledgeSpaceCorpus>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<MdlKnowledgeSpaceBinding>();
  const [form] = Form.useForm<BindingValues>();
  const modelNames = (Form.useWatch('modelNames', form) ?? []) as string[];
  const generation = useRef(0);
  const activeDatabase = useRef(database);
  activeDatabase.current = database;

  const load = useCallback(async () => {
    const current = ++generation.current;
    setCorpus(undefined);
    setLoading(Boolean(database));
    if (!database) return;
    try {
      const [error, data, response] = await apiInterceptors(getMdlKnowledgeSpaces(database));
      if (current !== generation.current) return;
      if (!error && response?.success && data) setCorpus(data);
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [database]);

  useEffect(() => {
    setOpen(false);
    setBusy(false);
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  const save = async (spaces: MdlKnowledgeSpaceBinding[]) => {
    if (!database || !corpus || busy) return false;
    const current = ++generation.current;
    setBusy(true);
    try {
      const [error, data, response] = await apiInterceptors(
        saveMdlKnowledgeSpaces({
          database,
          revision: corpus.revision,
          spaces: spaces.map(({ stale: _stale, ...binding }) => binding),
        }),
      );
      if (current !== generation.current || database !== activeDatabase.current) return false;
      if (error || !response?.success || !data) {
        message.error('绑定未保存，请检查字段及空间名称；版本冲突时刷新核对后重试。');
        return false;
      }
      setCorpus(data);
      return true;
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };

  const edit = (binding?: MdlKnowledgeSpaceBinding) => {
    setEditing(binding);
    form.resetFields();
    form.setFieldsValue(
      binding
        ? {
            ...binding,
            modelNames: Object.keys(binding.models),
            fieldsByModel: binding.models,
            usersText: binding.users.join('\n'),
          }
        : { modelNames: [], roles: [] },
    );
    setOpen(true);
  };

  const submit = async () => {
    const values = await form.validateFields();
    const bindings: Record<string, string[]> = {};
    for (const name of values.modelNames) {
      const fields = values.fieldsByModel?.[name] ?? [];
      if (!fields.length) {
        message.warning('每个绑定模型至少选择一个字段');
        return;
      }
      bindings[name] = fields;
    }
    const entry: MdlKnowledgeSpaceBinding = {
      id: editing?.id,
      title: values.title.trim(),
      space_name: values.space_name.trim(),
      description: values.description?.trim() ?? '',
      models: bindings,
      users: splitUsers(values.usersText),
      roles: values.roles ?? [],
      status: 'draft',
    };
    const spaces = editing
      ? (corpus?.spaces ?? []).map(item => (item.id === editing.id ? entry : item))
      : [...(corpus?.spaces ?? []), entry];
    if (await save(spaces)) {
      setOpen(false);
      message.success('绑定已保存，请审核后使用');
    }
  };

  const approve = async (entry: MdlKnowledgeSpaceBinding) => {
    if (!database || !corpus || !entry.id || busy) return;
    const current = ++generation.current;
    setBusy(true);
    try {
      const [error, data, response] = await apiInterceptors(
        approveMdlKnowledgeSpace({
          database,
          revision: corpus.revision,
          id: entry.id,
        }),
      );
      if (current !== generation.current || database !== activeDatabase.current) return;
      if (!error && response?.success && data) {
        setCorpus(data);
        message.success('知识空间绑定已审核');
      }
    } finally {
      if (current === generation.current) setBusy(false);
    }
  };

  const columns: ColumnsType<MdlKnowledgeSpaceBinding> = [
    {
      title: '知识空间',
      width: 220,
      render: (_, entry) => (
        <>
          <div>{entry.title}</div>
          <Typography.Text type='secondary'>{entry.space_name}</Typography.Text>
        </>
      ),
    },
    {
      title: '状态',
      width: 120,
      render: (_, entry) =>
        entry.stale ? (
          <Tag color='orange'>需重新审核</Tag>
        ) : entry.status === 'approved' ? (
          <Tag color='green'>已审核</Tag>
        ) : (
          <Tag>草稿</Tag>
        ),
    },
    {
      title: '绑定模型字段',
      width: 230,
      render: (_, entry) => (
        <Space wrap>
          {Object.entries(entry.models).map(([name, fields]) => (
            <Tag key={name}>
              {name}: {fields.length}
            </Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '适用账号',
      width: 160,
      render: (_, entry) => (
        <>
          <div>{entry.users.length ? entry.users.join('、') : '按字段权限筛选'}</div>
          <div>{entry.roles.join('、')}</div>
        </>
      ),
    },
    {
      title: '操作',
      width: 230,
      render: (_, entry) => (
        <Space>
          <Button type='link' icon={<EditOutlined />} disabled={busy} onClick={() => edit(entry)}>
            编辑
          </Button>
          <Button type='link' icon={<CheckCircleOutlined />} disabled={busy} onClick={() => approve(entry)}>
            审核
          </Button>
          <Button
            type='link'
            danger
            icon={<DeleteOutlined />}
            disabled={busy}
            onClick={() =>
              Modal.confirm({
                title: `删除「${entry.title}」的绑定？`,
                content: '移除绑定后问数不再检索该空间，原知识文档会保留。',
                onOk: () => save((corpus?.spaces ?? []).filter(item => item.id !== entry.id)).then(() => undefined),
              })
            }
          >
            删除
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <>
      <Card
        size='small'
        title={
          <Space>
            通用知识库绑定<Tag>版本 {corpus?.revision ?? '未加载'}</Tag>
          </Space>
        }
        extra={
          <Space wrap>
            <Button icon={<ReloadOutlined />} disabled={busy || loading} onClick={load}>
              刷新绑定
            </Button>
            <Button icon={<PlusOutlined />} disabled={!corpus || busy || loading} onClick={() => edit()}>
              绑定知识空间
            </Button>
          </Space>
        }
      >
        <Alert
          type='info'
          showIcon
          message='通用文档按当前数据源及模型字段授权检索'
          description='请绑定现有知识空间，并覆盖文档涉及的全部敏感字段。编辑绑定、MDL 或空间内容变化后需重新审核。'
          className='mb-3'
        />
        <Table
          rowKey='space_name'
          size='small'
          loading={loading || busy}
          dataSource={corpus?.spaces ?? []}
          columns={columns}
          scroll={{ x: 960 }}
          pagination={{ pageSize: 5 }}
        />
      </Card>
      <Modal
        title={editing ? '编辑知识空间绑定' : '绑定知识空间'}
        open={open}
        width={680}
        okText='保存绑定'
        confirmLoading={busy}
        onOk={submit}
        onCancel={() => setOpen(false)}
        destroyOnClose
      >
        <Form form={form} layout='vertical'>
          <Form.Item name='title' label='绑定名称' rules={[{ required: true, message: '请输入绑定名称' }]}>
            <Input />
          </Form.Item>
          <Form.Item name='space_name' label='现有知识空间名称' rules={[{ required: true, message: '请输入空间名称' }]}>
            <Input placeholder='在通用知识库页面创建的空间名称' />
          </Form.Item>
          <Form.Item name='description' label='适用说明'>
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name='modelNames' label='绑定模型' rules={[{ required: true, message: '请选择模型' }]}>
            <Select
              mode='multiple'
              options={models.map(model => ({ label: model.display_name || model.name, value: model.name }))}
            />
          </Form.Item>
          {modelNames.map(name => (
            <Form.Item key={name} name={['fieldsByModel', name]} label={`${name} 字段`}>
              <Select
                mode='multiple'
                options={(models.find(model => model.name === name)?.columns ?? []).map(column => ({
                  label: column.display_name ? `${column.display_name} (${column.name})` : column.name,
                  value: column.name,
                }))}
              />
            </Form.Item>
          ))}
          <Form.Item name='usersText' label='限定账号（可选）'>
            <Input.TextArea rows={2} placeholder='每行一个账号；留空时依据字段权限筛选' />
          </Form.Item>
          <Form.Item name='roles' label='限定角色（可选）'>
            <Select
              mode='multiple'
              options={[
                { label: '管理员', value: 'admin' },
                { label: '问数用户', value: 'analyst' },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
