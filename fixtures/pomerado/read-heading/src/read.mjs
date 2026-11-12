import {Schema} from 'effect';
import {defineOperation} from '../runtime/index.js';
export default defineOperation({name:'read_fixture',input:Schema.Struct({}),output:Schema.Struct({heading:Schema.String})}, async ({kernel,sessionId}) => {
 const response=await kernel.browsers.playwright.execute(sessionId,{code:"return await page.locator('h1').textContent();",timeout_sec:5});
 if(!response.success)throw new Error(String(response.error));
 return {heading:response.result};
});
