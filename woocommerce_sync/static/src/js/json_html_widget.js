/** @odoo-module **/

import { registry } from '@web/core/registry';
import { Component, markup } from '@odoo/owl';
import { standardFieldProps } from '@web/views/fields/standard_field_props';
import * as charFieldModule from '@web/views/fields/char/char_field';

// Plain Owl component: subclassing the web 'Field' wrapper crashes on Odoo 16, whose 'Field.setup()' reads 'props.fieldInfo' (not passed to field widgets)
export class JsonHtmlField extends Component {
  static template = 'woocommerce_sync.JsonHtmlField';
  static props = { ...standardFieldProps };
  static supportedTypes = ['json'];

  get formattedValue() {
    try {
      const value = this.props.record.data[this.props.name];
      if (value == null || value === '' || (typeof value === 'boolean' && value === false)) {
        return 'No data';
      }

      // parse stringified JSON, otherwise take it as is
      const jsonData = typeof value === 'string' ? JSON.parse(value) : value;
      return markup(this.formatJsonToTable(jsonData));
    } catch (e) {
      return 'Invalid JSON';
    }
  }

  formatJsonToTable(data) {
    const escapeHtml = (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');

    const tableStyle = `
            border-collapse: collapse;
            width: 100%;
            margin-bottom: 1em;
            font-family: Arial, sans-serif;
        `;
    const thStyle = `
            background-color: #f2f2f2;
            text-align: left;
            padding: 8px;
            font-weight: bold;
        `;
    const tdStyle = `
            border: 1px solid #ddd;
            padding: 8px;
            text-align: left;
            vertical-align: top;
        `;

    const renderTable = (headers, rows) => `
        <table style="${tableStyle}">
            <thead>
                <tr>${headers.map((header) => `<th style="${thStyle}">${escapeHtml(header)}</th>`).join('')}</tr>
        </thead>
        <tbody>
        ${rows
          .map(
            (cells) => `
                    <tr>${cells.map((cell) => `<td style="${tdStyle}">${this.formatJsonToTable(cell)}</td>`).join('')}</tr>
            `
          )
          .join('')}
            </tbody>
        </table>
        `;

    if (Array.isArray(data)) {
      if (data.length === 0) return '[]';

      const objectKeys = Array.from(new Set(data.flatMap((item) => (item && typeof item === 'object' ? Object.keys(item) : []))));

      if (objectKeys.length > 0) {
        const rows = data.map((item) => objectKeys.map((key) => item?.[key] ?? ''));
        return renderTable(objectKeys, rows);
      } else {
        const rows = data.map((value, index) => [index, value]);
        return renderTable(['Index', 'Value'], rows);
      }
    }

    if (data && typeof data === 'object') {
      const entries = Object.entries(data);
      if (entries.length === 0) return '{}';
      const headers = ['Key', 'Value'];
      const rows = entries.map(([k, v]) => [k, v]);
      return renderTable(headers, rows);
    }

    if (data === null) return 'null';
    if (typeof data === 'boolean') return data ? 'true' : 'false';
    return escapeHtml(data);
  }
}

// Odoo 17+ field registry entries are '{ component }' descriptors (with a matching 'charField' export), Odoo 16 entries are the component class itself.
// Detected from the char field module instead of the registry content, which depends on the asset loading order (on Odoo 16 'char' may not be registered yet)
if ('charField' in charFieldModule) {
  registry.category('fields').add('json_html', { component: JsonHtmlField, supportedTypes: ['json'] });
} else {
  registry.category('fields').add('json_html', JsonHtmlField);
}
