import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {it,expect,vi} from 'vitest';
import CaptionFinishing from '../src/ui/CaptionFinishing';
import type {DesktopBridge} from '../src/bridge';
import type {TimelineDocument} from '../src/contracts';

it('generates captions with the current revision and surfaces readability warnings',async()=>{
 const request=vi.fn(async()=>({captionCount:2,timeline:{revision:8},warnings:[{message:'Reading speed exceeds the limit.'}]})),saved=vi.fn();
 render(<CaptionFinishing bridge={{request} as unknown as DesktopBridge} path="C:/project" doc={{revision:7} as TimelineDocument} onSaved={saved}/>);
 fireEvent.change(screen.getByLabelText('Characters per line'),{target:{value:'32'}});
 fireEvent.click(screen.getByText('Generate editable captions'));
 await screen.findByText(/2 captions generated.*Reading speed exceeds/);
 await waitFor(()=>expect(saved).toHaveBeenCalledOnce());
 expect(request).toHaveBeenCalledWith('caption_generate',expect.objectContaining({expectedRevision:7,settings:expect.objectContaining({lineLength:32})}));
});
