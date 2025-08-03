/** @odoo-module **/

import { registry } from "@web/core/registry";
import { markup, useRef } from "@odoo/owl";
import { Field } from "@web/views/fields/field";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

export class JsonHtmlField extends Field {
    static template = "woocommerce_sync.JsonHtmlField";
    static props = { ...standardFieldProps };
    static supportedTypes = ["json"];

    setup() {
        super.setup(...arguments);
        this.input = useRef("jsonInput");
    }

    get formattedValue() {
        try {
            const value = this.props.record.data[this.props.name];
            if (value == null || value === "" || (typeof value === 'boolean' && value === false)) {
                return "No data";
            }

            // parse stringified JSON, otherwise take it as is
            const jsonData = typeof value === "string" ? JSON.parse(value) : value;
            return markup(this.formatJsonToTable(jsonData));
        } catch (e) {
            return "Invalid JSON";
        }
    }

    formatJsonToTable(data) {
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
                <tr>${headers.map(h => `<th style="${thStyle}">${h}</th>`).join('')}</tr>
        </thead>
        <tbody>
        ${rows.map(cells => `
                    <tr>${cells.map(cell => `<td style="${tdStyle}">${this.formatJsonToTable(cell)}</td>`).join('')}</tr>
            `).join('')}
            </tbody>
        </table>
        `;

            if (Array.isArray(data)) {
                if (data.length === 0) return "[]";

                const objectKeys = Array.from(new Set(
                    data.flatMap(item => (item && typeof item === 'object') ? Object.keys(item) : [])
                ));

                if (objectKeys.length > 0) {
                    const rows = data.map(item =>
                        objectKeys.map(key => item?.[key] ?? "")
                    );
                    return renderTable(objectKeys, rows);
                } else {
                    const rows = data.map((value, index) => [index, value]);
                    return renderTable(["Index", "Value"], rows);
                }
            }

            if (data && typeof data === "object") {
                const entries = Object.entries(data);
                if (entries.length === 0) return "{}";
                const headers = ["Key", "Value"];
                const rows = entries.map(([k, v]) => [k, v]);
                return renderTable(headers, rows);
            }

            if (data === null) return "null";
            if (typeof data === "boolean") return data ? "true" : "false";
            return String(data);
        }
        }


        if (!!registry.category("fields").get("char")?.component) {
    // Odoo 17+
            registry.category("fields").add("json_html", { component: JsonHtmlField });
        } else {
    // Odoo 16 and below
            registry.category("fields").add("json_html", JsonHtmlField);
        }
